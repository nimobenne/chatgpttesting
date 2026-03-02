#!/usr/bin/env python3
<<<<<<< ours
<<<<<<< ours
"""Live terminal dashboard for active Claude Code agents.

Scans Claude debug logs, correlates sessions with running Claude-related processes,
and renders a live updating Rich TUI.
"""
=======
"""Live terminal dashboard for monitoring active Claude Code agents without external deps."""
>>>>>>> theirs
=======
"""Live terminal dashboard for monitoring active Claude Code agents without external deps."""
>>>>>>> theirs

from __future__ import annotations

import argparse
<<<<<<< ours
<<<<<<< ours
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
=======
=======
>>>>>>> theirs
import datetime as dt
import curses
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

STATE_PATTERNS: List[Tuple[re.Pattern[str], str]] = [
    (re.compile(r"\btyping\b", re.IGNORECASE), "typing"),
    (re.compile(r"\breading\b", re.IGNORECASE), "reading"),
    (re.compile(r"\bidle\b", re.IGNORECASE), "idle"),
    (re.compile(r"\bsleep(?:ing)?\b", re.IGNORECASE), "sleep"),
    (re.compile(r"wait(?:ing)?\s+for\s+input", re.IGNORECASE), "waiting for input"),
]

TOOL_PATTERN = re.compile(
    r"(?:tool|using tool|invoke(?:d)? tool|call(?:ed)? tool)\s*[:=]\s*['\"]?([a-zA-Z0-9_.\-/]+)",
    re.IGNORECASE,
)
SESSION_PATTERN = re.compile(r"(?:session(?:_id| id)?|sid)\s*[:=]\s*([a-zA-Z0-9\-_:./]+)", re.IGNORECASE)
PROJECT_PATTERN = re.compile(
    r"(?:project|repo|workspace|cwd|working dir(?:ectory)?)\s*[:=]\s*['\"]?([^'\"\n]+)",
    re.IGNORECASE,
)
PID_PATTERN = re.compile(r"(?:pid|process(?:_id)?)\s*[:=]\s*(\d+)", re.IGNORECASE)


@dataclass
class AgentSnapshot:
    session_id: str
    pid: Optional[int]
    project: str
    state: str
    tool: str
    started_at: Optional[float]
    is_running: bool

    @property
    def runtime(self) -> str:
        if not self.started_at:
            return "unknown"
        elapsed = max(0, int(time.time() - self.started_at))
        hours, rem = divmod(elapsed, 3600)
        minutes, seconds = divmod(rem, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor active Claude Code agents in a live TUI.")
    parser.add_argument("--scan-path", action="append", default=[], help="Path to scan for debug logs.")
    parser.add_argument("--refresh", type=float, default=1.0, help="Refresh interval in seconds.")
    parser.add_argument("--max-lines", type=int, default=2000, help="Max tail lines to parse per log file.")
    return parser.parse_args()


def default_scan_paths() -> List[Path]:
    candidates = [
        Path("/.cladude.debug"),
        Path("/.claude.debug"),
        Path.home() / ".cladude.debug",
        Path.home() / ".claude.debug",
        Path.home() / ".config" / "claude",
    ]
    return [p for p in candidates if p.exists()]


def gather_log_files(scan_paths: Iterable[Path]) -> List[Path]:
    logs: List[Path] = []
    for path in scan_paths:
        if path.is_file() and path.suffix in {".log", ".jsonl", ".txt"}:
            logs.append(path)
        elif path.is_dir():
            for file in path.rglob("*"):
                if file.is_file() and file.suffix in {".log", ".jsonl", ".txt"}:
                    logs.append(file)
    return sorted(set(logs))


def tail_lines(path: Path, max_lines: int) -> List[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    return lines[-max_lines:] if len(lines) > max_lines else lines


def first_match(pattern: re.Pattern[str], lines: Iterable[str], reverse: bool = False) -> Optional[str]:
    sequence = list(lines)
    if reverse:
        sequence.reverse()
    for line in sequence:
        match = pattern.search(line)
        if match:
            return match.group(1).strip()
    return None


def detect_state(lines: Iterable[str]) -> str:
    for line in reversed(list(lines)):
        for pattern, state in STATE_PATTERNS:
            if pattern.search(line):
                return state
    return "unknown"


def detect_start_timestamp(lines: Iterable[str]) -> Optional[float]:
    ts_patterns = [
        re.compile(r"(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"),
        re.compile(r"\b(\d{10})\b"),
    ]
    for line in lines:
        for pattern in ts_patterns:
            match = pattern.search(line)
            if not match:
                continue
            raw = match.group(1)
            if raw.isdigit() and len(raw) == 10:
                return float(raw)
            try:
                return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
    return None


def running_claude_pids() -> Dict[int, str]:
    try:
        proc = subprocess.run(["ps", "-eo", "pid=,args="], check=True, text=True, capture_output=True)
    except subprocess.SubprocessError:
        return {}
    pids: Dict[int, str] = {}
    for row in proc.stdout.splitlines():
        row = row.strip()
        if not row:
            continue
        pid_part, *rest = row.split(maxsplit=1)
        try:
            pid = int(pid_part)
        except ValueError:
            continue
        args = rest[0] if rest else ""
        if "claude" in args.lower():
            pids[pid] = args
    return pids


def parse_agent_from_log(path: Path, max_lines: int, live_pids: Dict[int, str]) -> Optional[AgentSnapshot]:
    lines = tail_lines(path, max_lines)
    if not lines:
        return None

    session_id = first_match(SESSION_PATTERN, lines, reverse=True) or path.stem
    pid_text = first_match(PID_PATTERN, lines, reverse=True)
    pid = int(pid_text) if pid_text and pid_text.isdigit() else None

    project = first_match(PROJECT_PATTERN, lines, reverse=True)
    if not project and pid and pid in live_pids:
        cmd = live_pids[pid]
        match = re.search(r"(?:--cwd|--project)\s+([^\s]+)", cmd)
        project = match.group(1) if match else "unknown"
    project = project or "unknown"

    state = detect_state(lines)
    tool = first_match(TOOL_PATTERN, lines, reverse=True) or "-"
    started_at = detect_start_timestamp(lines)

    is_running = bool(pid and pid in live_pids)
    if not pid:
        stem_digits = re.search(r"(\d{3,})", path.stem)
        if stem_digits:
            possible_pid = int(stem_digits.group(1))
            if possible_pid in live_pids:
                pid = possible_pid
                is_running = True

    return AgentSnapshot(session_id, pid, project, state, tool, started_at, is_running)


def collect_agents(scan_paths: List[Path], max_lines: int) -> List[AgentSnapshot]:
    live_pids = running_claude_pids()
    snapshots: Dict[str, AgentSnapshot] = {}
    for log in gather_log_files(scan_paths):
        snap = parse_agent_from_log(log, max_lines, live_pids)
        if snap and (snap.is_running or snap.state != "unknown"):
            snapshots[snap.session_id] = snap
    return sorted(snapshots.values(), key=lambda s: (not s.is_running, s.session_id))


def fit(value: str, width: int) -> str:
    if width <= 1:
        return ""
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return value[: width - 1] + "…"


def draw_dashboard(stdscr: "curses._CursesWindow", scan_paths: List[Path], refresh: float, max_lines: int) -> None:
    curses.curs_set(0)
    stdscr.nodelay(True)

    headers = ["Session ID", "PID", "Project", "State", "Tool", "Runtime", "Status"]

    while True:
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        snapshots = collect_agents(scan_paths, max_lines)

        stdscr.addstr(0, 0, fit("Claude Code Agents Monitor (q to quit)", w - 1))
        stdscr.addstr(1, 0, fit(f"Scanning: {', '.join(str(p) for p in scan_paths)}", w - 1))

        widths = [24, 7, 24, 18, 18, 10, 10]
        total = sum(widths) + len(widths) - 1
        if total > w - 1:
            scale = (w - 1 - (len(widths) - 1)) / max(sum(widths), 1)
            widths = [max(4, int(col * scale)) for col in widths]

        y = 3
        x = 0
        for idx, head in enumerate(headers):
            stdscr.addstr(y, x, fit(head, widths[idx]))
            x += widths[idx] + 1

        y += 1
        stdscr.hline(y, 0, ord("-"), min(w - 1, sum(widths) + len(widths) - 1))
        y += 1

        if not snapshots:
            stdscr.addstr(y, 0, fit("No agents detected", w - 1))
        else:
            for snap in snapshots:
                if y >= h - 1:
                    break
                row = [
                    snap.session_id,
                    str(snap.pid or "-"),
                    snap.project,
                    snap.state,
                    snap.tool,
                    snap.runtime,
                    "running" if snap.is_running else "log-only",
                ]
                x = 0
                for idx, col in enumerate(row):
                    stdscr.addstr(y, x, fit(col, widths[idx]))
                    x += widths[idx] + 1
                y += 1

        stdscr.refresh()

        for _ in range(max(1, int(refresh * 10))):
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                return
            time.sleep(0.1)




def print_snapshot(scan_paths: List[Path], max_lines: int) -> None:
    snapshots = collect_agents(scan_paths, max_lines)
    print("Claude Code Agents Monitor")
    print(f"Scanning: {', '.join(str(p) for p in scan_paths)}")
    if not snapshots:
        print("No agents detected")
        return
    for snap in snapshots:
        print(
            f"{snap.session_id} | pid={snap.pid or '-'} | project={snap.project} | "
            f"state={snap.state} | tool={snap.tool} | runtime={snap.runtime} | "
            f"status={'running' if snap.is_running else 'log-only'}"
        )

def main() -> int:
    args = parse_args()
    scan_paths = [Path(p).expanduser() for p in args.scan_path] if args.scan_path else default_scan_paths()
    if not scan_paths:
        print("No debug paths found. Use --scan-path to provide log directories.")
        return 1
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print_snapshot(scan_paths, args.max_lines)
        return 0
    curses.wrapper(draw_dashboard, scan_paths, max(args.refresh, 0.2), args.max_lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
<<<<<<< ours
>>>>>>> theirs
=======
>>>>>>> theirs
