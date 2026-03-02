#!/usr/bin/env python3
"""Live terminal dashboard for active Claude Code agents.

Scans Claude debug logs, correlates sessions with running Claude-related processes,
and renders a live updating Rich TUI.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from rich import box
from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

STATE_ORDER = ["typing", "reading", "waiting", "idle", "sleep", "unknown"]
STATE_COLORS = {
    "typing": "green",
    "reading": "cyan",
    "waiting": "yellow",
    "idle": "magenta",
    "sleep": "dim",
    "unknown": "white",
}

STATE_PATTERNS = {
    "typing": ["typing", "stream", "generating", "responding"],
    "reading": ["reading", "analyzing", "parse", "indexing"],
    "waiting": ["wait", "input", "prompt", "blocked"],
    "idle": ["idle", "ready", "done", "complete"],
    "sleep": ["sleep", "paused", "backoff", "throttle"],
}

TOOL_REGEX = re.compile(r"\btool\b\s*[:=]\s*([\w.-]+)", re.IGNORECASE)
SESSION_REGEX = re.compile(r"\b(session(?:_id)?|sid)\b\s*[:=]\s*([\w-]+)", re.IGNORECASE)
PROJECT_REGEX = re.compile(r"\b(project|cwd|workspace)\b\s*[:=]\s*([^\s,;]+)", re.IGNORECASE)


@dataclass
class ProcessInfo:
    pid: int
    etimes: int
    cmd: str


@dataclass
class AgentSession:
    session_id: str
    project: str = "-"
    state: str = "unknown"
    tool: str = "-"
    pid: Optional[int] = None
    runtime_seconds: int = 0
    last_seen: float = field(default_factory=time.time)
    source_log: str = "-"


class LogTailer:
    def __init__(self, path: Path):
        self.path = path
        self.offset = 0
        self.inode = None

    def read_new_lines(self) -> List[str]:
        if not self.path.exists():
            return []

        stat = self.path.stat()
        if self.inode is None or self.inode != stat.st_ino or stat.st_size < self.offset:
            self.inode = stat.st_ino
            self.offset = 0

        lines: List[str] = []
        with self.path.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(self.offset)
            for line in f:
                lines.append(line.rstrip("\n"))
            self.offset = f.tell()
        return lines


class AgentMonitor:
    def __init__(self, scan_roots: List[Path]):
        self.scan_roots = scan_roots
        self.tailers: Dict[Path, LogTailer] = {}
        self.sessions: Dict[str, AgentSession] = {}

    def discover_logs(self) -> None:
        candidates: List[Path] = []
        for root in self.scan_roots:
            if not root.exists():
                continue
            if root.is_file():
                name = root.name.lower()
                if ("claude" in name or "cladude" in name) and "debug" in name:
                    candidates.append(root)
                continue
            for pattern in ["*.log", "*.txt", "*.ndjson", "*"]:
                for p in root.rglob(pattern):
                    name = p.name.lower()
                    if p.is_file() and ("claude" in name or "cladude" in name) and "debug" in name:
                        candidates.append(p)

        for path in candidates:
            if path not in self.tailers:
                self.tailers[path] = LogTailer(path)

    def refresh(self) -> None:
        self.discover_logs()
        for path, tailer in list(self.tailers.items()):
            for line in tailer.read_new_lines():
                self._ingest_line(line, path)

        self._correlate_processes()
        self._prune_inactive()

    def _ingest_line(self, line: str, source: Path) -> None:
        payload = self._parse_payload(line)
        session_id = payload.get("session_id") or self._find_regex(SESSION_REGEX, line, 2)
        if not session_id:
            return

        session = self.sessions.setdefault(session_id, AgentSession(session_id=session_id))
        session.last_seen = time.time()
        session.source_log = str(source)

        project = payload.get("project") or payload.get("cwd") or self._find_regex(PROJECT_REGEX, line, 2)
        if project:
            session.project = project

        tool = payload.get("tool") or self._extract_tool(line)
        if tool:
            session.tool = tool

        pid = payload.get("pid")
        if isinstance(pid, int):
            session.pid = pid

        state = self._infer_state(payload, line)
        if state:
            session.state = state

    @staticmethod
    def _parse_payload(line: str) -> Dict[str, object]:
        line = line.strip()
        if not line:
            return {}
        if line.startswith("{") and line.endswith("}"):
            try:
                data = json.loads(line)
                if isinstance(data, dict):
                    return data
            except json.JSONDecodeError:
                return {}
        return {}

    @staticmethod
    def _find_regex(pattern: re.Pattern[str], text: str, group: int) -> Optional[str]:
        match = pattern.search(text)
        if match:
            return match.group(group)
        return None

    @staticmethod
    def _extract_tool(line: str) -> Optional[str]:
        match = TOOL_REGEX.search(line)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _infer_state(payload: Dict[str, object], line: str) -> str:
        candidates = [
            str(payload.get("state", "")),
            str(payload.get("status", "")),
            str(payload.get("event", "")),
            line,
        ]
        blob = " ".join(candidates).lower()
        for state in STATE_ORDER:
            if state == "unknown":
                continue
            for token in STATE_PATTERNS[state]:
                if token in blob:
                    return state
        return "unknown"

    def _correlate_processes(self) -> None:
        processes = list_claude_processes()
        by_pid = {proc.pid: proc for proc in processes}

        for session in self.sessions.values():
            if session.pid and session.pid in by_pid:
                session.runtime_seconds = by_pid[session.pid].etimes
                continue

            chosen = self._pick_process_for_session(session, processes)
            if chosen:
                session.pid = chosen.pid
                session.runtime_seconds = chosen.etimes

    @staticmethod
    def _pick_process_for_session(session: AgentSession, processes: List[ProcessInfo]) -> Optional[ProcessInfo]:
        sid = session.session_id.lower()
        project = session.project.lower()

        for proc in processes:
            cmd = proc.cmd.lower()
            if sid in cmd:
                return proc
        for proc in processes:
            cmd = proc.cmd.lower()
            if project and project != "-" and project in cmd:
                return proc
        return None

    def _prune_inactive(self, ttl: int = 1800) -> None:
        now = time.time()
        to_delete = [sid for sid, sess in self.sessions.items() if now - sess.last_seen > ttl]
        for sid in to_delete:
            del self.sessions[sid]


def list_claude_processes() -> List[ProcessInfo]:
    cmd = ["ps", "-eo", "pid=,etimes=,args="]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return []

    procs: List[ProcessInfo] = []
    for raw in result.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(maxsplit=2)
        if len(parts) != 3:
            continue
        pid_s, etimes_s, args = parts
        if "claude" not in args.lower():
            continue
        try:
            procs.append(ProcessInfo(pid=int(pid_s), etimes=int(etimes_s), cmd=args))
        except ValueError:
            continue
    return procs


def format_duration(seconds: int) -> str:
    h, r = divmod(seconds, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def build_table(sessions: Iterable[AgentSession]) -> Table:
    table = Table(title="Claude Agent Monitor", box=box.SIMPLE_HEAVY)
    table.add_column("Session ID", style="bold")
    table.add_column("Project")
    table.add_column("State")
    table.add_column("Tool")
    table.add_column("PID", justify="right")
    table.add_column("Running", justify="right")

    ordered = sorted(sessions, key=lambda s: (STATE_ORDER.index(s.state) if s.state in STATE_ORDER else 99, s.session_id))
    if not ordered:
        table.add_row("-", "No active sessions", "-", "-", "-", "-")
        return table

    for s in ordered:
        color = STATE_COLORS.get(s.state, "white")
        state = Text(s.state, style=color)
        table.add_row(
            s.session_id,
            s.project,
            state,
            s.tool,
            str(s.pid) if s.pid else "-",
            format_duration(s.runtime_seconds),
        )
    return table


def build_footer(monitor: AgentMonitor) -> Panel:
    logs = ", ".join(str(path) for path in monitor.tailers.keys()) or "none found"
    content = Text(f"Watching logs: {logs}")
    return Panel(content, title="Sources")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live dashboard for Claude code agents")
    parser.add_argument("--refresh", type=float, default=1.0, help="Refresh interval in seconds")
    parser.add_argument(
        "--scan-root",
        action="append",
        default=[],
        help="Directory to scan recursively for Claude debug logs (can be repeated)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    roots = [Path(p).expanduser() for p in args.scan_root]
    if not roots:
        roots = [Path("/.cladude.debug"), Path("~/.claude").expanduser(), Path("/")]

    monitor = AgentMonitor(roots)
    with Live(refresh_per_second=max(int(1 / max(args.refresh, 0.2)), 1), screen=True) as live:
        while True:
            monitor.refresh()
            group = Group(build_table(monitor.sessions.values()), build_footer(monitor))
            live.update(group)
            time.sleep(args.refresh)


if __name__ == "__main__":
    main()
