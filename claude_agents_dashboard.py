#!/usr/bin/env python3
"""Live terminal dashboard for Codex/Claude agent activity.

This dashboard scans local debug logs and correlates them with running processes.
It highlights agent activity states like typing, thinking, idle, and waiting,
and shows timing/status details in a clean terminal table.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional

LOG_EXTENSIONS = {".log", ".txt", ".jsonl", ".ndjson"}
LOG_NAME_HINTS = ("codex", "claude", "agent", "debug")

STATE_RULES = [
    ("typing", re.compile(r"\b(typing|stream(ing)?|generat(ing|ed)?|respond(ing|ed)?)\b", re.IGNORECASE)),
    ("thinking", re.compile(r"\b(thinking|analy(s|z)ing|reason(ing|ed)?|planning|reflect(ing|ed)?)\b", re.IGNORECASE)),
    ("reading", re.compile(r"\b(reading|parsing|indexing|inspect(ing|ed)?)\b", re.IGNORECASE)),
    ("waiting", re.compile(r"\b(wait(ing)?|blocked|await(ing)?\s+input|need\s+input)\b", re.IGNORECASE)),
    ("idle", re.compile(r"\b(idle|ready|complete(d)?|done)\b", re.IGNORECASE)),
    ("sleep", re.compile(r"\b(sleep(ing)?|paused|backoff|throttl(ed|ing)?)\b", re.IGNORECASE)),
]

SESSION_RE = re.compile(r"\b(?:session(?:_id|\s*id)?|sid)\b\s*[:=]\s*['\"]?([a-zA-Z0-9_.:/\-]+)", re.IGNORECASE)
PROJECT_RE = re.compile(r"\b(?:project|repo|workspace|cwd|workdir)\b\s*[:=]\s*['\"]?([^'\"\s,;]+)", re.IGNORECASE)
TOOL_RE = re.compile(r"\b(?:tool|using\s+tool|invoke(?:d)?\s+tool|call(?:ed)?\s+tool)\b\s*[:=]\s*['\"]?([a-zA-Z0-9_.\-/]+)", re.IGNORECASE)
PID_RE = re.compile(r"\b(?:pid|process(?:_id)?)\b\s*[:=]\s*(\d+)", re.IGNORECASE)
ISO_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")
UNIX_TS_RE = re.compile(r"\b(\d{10})\b")


@dataclass
class ProcessInfo:
    pid: int
    name: str
    cmd: str
    created_at: Optional[float] = None


@dataclass
class AgentSession:
    session_id: str
    project: str = "-"
    tool: str = "-"
    state: str = "unknown"
    status: str = "unknown"
    pid: Optional[int] = None
    process_name: str = "-"
    process_cmd: str = ""
    started_at: Optional[float] = None
    last_event_at: Optional[float] = None
    last_seen_at: float = field(default_factory=time.time)
    source_file: str = "-"


@dataclass
class TailState:
    offset: int = 0
    signature: Optional[tuple[int, int]] = None


class LogTailer:
    def __init__(self) -> None:
        self._states: Dict[Path, TailState] = {}

    def read_new_lines(self, path: Path, max_lines: int) -> List[str]:
        if not path.exists() or not path.is_file():
            return []

        try:
            stat = path.stat()
        except OSError:
            return []

        signature = (int(stat.st_mtime_ns), int(stat.st_size))
        state = self._states.setdefault(path, TailState())

        if state.signature is None:
            # First read: tail the latest window instead of full file.
            lines = read_tail_lines(path, max_lines)
            state.offset = stat.st_size
            state.signature = signature
            return lines

        if stat.st_size < state.offset:
            # File rotated/truncated.
            state.offset = 0

        lines: List[str] = []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(state.offset)
                for raw in handle:
                    lines.append(raw.rstrip("\n"))
                state.offset = handle.tell()
                state.signature = signature
        except OSError:
            return []

        if len(lines) > max_lines:
            return lines[-max_lines:]
        return lines


def default_scan_paths() -> List[Path]:
    home = Path.home()
    candidates = [
        Path.cwd(),
        home / ".codex",
        home / ".claude",
        home / ".claude.debug",
        home / ".cladude.debug",
        home / ".config" / "claude",
    ]

    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        local_appdata = os.environ.get("LOCALAPPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Claude")
            candidates.append(Path(appdata) / "Codex")
        if local_appdata:
            candidates.append(Path(local_appdata) / "Claude")
            candidates.append(Path(local_appdata) / "Codex")

    unique: List[Path] = []
    seen = set()
    for path in candidates:
        key = str(path)
        if key not in seen and path.exists():
            seen.add(key)
            unique.append(path)
    return unique


def discover_log_files(scan_paths: Iterable[Path]) -> List[Path]:
    files: List[Path] = []
    for root in scan_paths:
        if not root.exists():
            continue
        if root.is_file():
            name = root.name.lower()
            if root.suffix.lower() in LOG_EXTENSIONS or any(h in name for h in LOG_NAME_HINTS):
                files.append(root)
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            name = path.name.lower()
            if path.suffix.lower() in LOG_EXTENSIONS and any(h in name for h in LOG_NAME_HINTS):
                files.append(path)
    return sorted(set(files))


def read_tail_lines(path: Path, max_lines: int) -> List[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return []
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if len(lines) > max_lines:
        return lines[-max_lines:]
    return lines


def parse_time_from_line(line: str) -> Optional[float]:
    iso = ISO_TS_RE.search(line)
    if iso:
        raw = iso.group(1)
        try:
            return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass

    unix_match = UNIX_TS_RE.search(line)
    if unix_match:
        try:
            return float(unix_match.group(1))
        except ValueError:
            return None
    return None


def first_match(pattern: re.Pattern[str], text: str) -> Optional[str]:
    match = pattern.search(text)
    return match.group(1).strip() if match else None


def detect_state(payload_text: str) -> str:
    for name, pattern in STATE_RULES:
        if pattern.search(payload_text):
            return name
    return "unknown"


def running_processes() -> Dict[int, ProcessInfo]:
    return _running_processes_windows() if os.name == "nt" else _running_processes_posix()


def _running_processes_windows() -> Dict[int, ProcessInfo]:
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine,CreationDate | ConvertTo-Json -Depth 2 -Compress",
    ]
    try:
        out = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.SubprocessError:
        return {}

    raw = out.stdout.strip()
    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    rows = data if isinstance(data, list) else [data]
    procs: Dict[int, ProcessInfo] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = row.get("ProcessId")
        if not isinstance(pid, int):
            continue
        name = str(row.get("Name") or "")
        cmd = str(row.get("CommandLine") or "")
        blob = f"{name} {cmd}".lower()
        if "codex" not in blob and "claude" not in blob:
            continue
        created_at = None
        creation = row.get("CreationDate")
        if isinstance(creation, str) and len(creation) >= 14:
            # WMI format like 20260302235901.123456+060
            try:
                parsed = dt.datetime.strptime(creation[:14], "%Y%m%d%H%M%S")
                created_at = parsed.timestamp()
            except ValueError:
                created_at = None

        procs[pid] = ProcessInfo(pid=pid, name=name or "process", cmd=cmd, created_at=created_at)
    return procs


def _running_processes_posix() -> Dict[int, ProcessInfo]:
    command = ["ps", "-eo", "pid=,lstart=,args="]
    try:
        out = subprocess.run(command, check=True, capture_output=True, text=True)
    except subprocess.SubprocessError:
        return {}

    procs: Dict[int, ProcessInfo] = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(maxsplit=6)
        if len(parts) < 7:
            continue
        pid_raw = parts[0]
        lstart = " ".join(parts[1:6])
        cmd = parts[6]
        try:
            pid = int(pid_raw)
        except ValueError:
            continue
        blob = cmd.lower()
        if "codex" not in blob and "claude" not in blob:
            continue
        created_at = None
        try:
            created_at = dt.datetime.strptime(lstart, "%a %b %d %H:%M:%S %Y").timestamp()
        except ValueError:
            created_at = None
        procs[pid] = ProcessInfo(pid=pid, name=Path(cmd.split()[0]).name, cmd=cmd, created_at=created_at)
    return procs


class AgentDashboard:
    def __init__(self, scan_paths: List[Path], refresh: float, max_lines: int, stale_seconds: int) -> None:
        self.scan_paths = scan_paths
        self.refresh = max(refresh, 0.2)
        self.max_lines = max_lines
        self.stale_seconds = stale_seconds
        self.tailer = LogTailer()
        self.sessions: Dict[str, AgentSession] = {}
        self.log_files: List[Path] = []

    def refresh_once(self) -> None:
        self.log_files = discover_log_files(self.scan_paths)

        for log_file in self.log_files:
            lines = self.tailer.read_new_lines(log_file, self.max_lines)
            if not lines:
                continue
            for line in lines:
                self._ingest_line(line, log_file)

        procs = running_processes()
        self._correlate_processes(procs)
        self._compute_statuses()
        self._prune_stale()

    def _ingest_line(self, line: str, source: Path) -> None:
        if not line.strip():
            return

        payload_text = line
        if line.lstrip().startswith("{") and line.rstrip().endswith("}"):
            try:
                obj = json.loads(line)
                if isinstance(obj, dict):
                    payload_text = " ".join(f"{k}:{v}" for k, v in obj.items())
            except json.JSONDecodeError:
                pass

        session_id = first_match(SESSION_RE, payload_text)
        if not session_id:
            return

        session = self.sessions.setdefault(session_id, AgentSession(session_id=session_id))
        now = time.time()
        session.last_seen_at = now
        session.source_file = str(source)

        project = first_match(PROJECT_RE, payload_text)
        if project:
            session.project = project

        tool = first_match(TOOL_RE, payload_text)
        if tool:
            session.tool = tool

        pid_txt = first_match(PID_RE, payload_text)
        if pid_txt and pid_txt.isdigit():
            session.pid = int(pid_txt)

        parsed_ts = parse_time_from_line(line)
        if parsed_ts:
            session.last_event_at = parsed_ts
            if session.started_at is None:
                session.started_at = parsed_ts
        else:
            session.last_event_at = now if session.last_event_at is None else session.last_event_at
            if session.started_at is None:
                session.started_at = now

        state = detect_state(payload_text)
        if state != "unknown":
            session.state = state

    def _correlate_processes(self, procs: Dict[int, ProcessInfo]) -> None:
        for session in self.sessions.values():
            if session.pid and session.pid in procs:
                proc = procs[session.pid]
                session.process_name = proc.name
                session.process_cmd = proc.cmd
                if proc.created_at:
                    session.started_at = proc.created_at
                continue

            chosen = self._pick_process(session, procs)
            if chosen:
                session.pid = chosen.pid
                session.process_name = chosen.name
                session.process_cmd = chosen.cmd
                if chosen.created_at:
                    session.started_at = chosen.created_at

    @staticmethod
    def _pick_process(session: AgentSession, procs: Dict[int, ProcessInfo]) -> Optional[ProcessInfo]:
        sid = session.session_id.lower()
        project = session.project.lower()

        for proc in procs.values():
            cmd = proc.cmd.lower()
            if sid and sid in cmd:
                return proc

        if project and project != "-":
            for proc in procs.values():
                if project in proc.cmd.lower():
                    return proc

        return None

    def _compute_statuses(self) -> None:
        now = time.time()
        process_pids = {s.pid for s in self.sessions.values() if s.pid}

        for session in self.sessions.values():
            running = bool(session.pid and session.pid in process_pids)
            age = now - (session.last_event_at or session.last_seen_at)

            if running and age <= 30:
                session.status = "active"
            elif running and session.state == "idle":
                session.status = "running-idle"
            elif running:
                session.status = "running"
            elif age <= 60:
                session.status = "recent-log"
            else:
                session.status = "stale"

    def _prune_stale(self) -> None:
        now = time.time()
        dead = [
            sid
            for sid, sess in self.sessions.items()
            if sess.status == "stale" and (now - sess.last_seen_at) > self.stale_seconds
        ]
        for sid in dead:
            del self.sessions[sid]

    def snapshots(self) -> List[AgentSession]:
        def sort_key(sess: AgentSession) -> tuple[int, str]:
            priority = {
                "active": 0,
                "running": 1,
                "running-idle": 2,
                "recent-log": 3,
                "stale": 4,
                "unknown": 5,
            }.get(sess.status, 9)
            return (priority, sess.session_id)

        return sorted(self.sessions.values(), key=sort_key)


def format_age(seconds: Optional[float]) -> str:
    if seconds is None:
        return "-"
    value = max(0, int(seconds))
    h, rem = divmod(value, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def clip(value: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return value[: width - 3] + "..."


def clear_screen() -> None:
    # ANSI clear + home. Works in modern Windows terminals and POSIX terminals.
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


def render_dashboard(dashboard: AgentDashboard) -> str:
    rows = dashboard.snapshots()
    now = time.time()

    active = sum(1 for s in rows if s.status == "active")
    running = sum(1 for s in rows if s.status in {"active", "running", "running-idle"})
    thinking = sum(1 for s in rows if s.state == "thinking")
    typing = sum(1 for s in rows if s.state == "typing")
    idle = sum(1 for s in rows if s.state == "idle")

    lines: List[str] = []
    lines.append("Codex / Claude Activity Dashboard")
    lines.append(
        f"time={dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | sessions={len(rows)} | "
        f"active={active} | running={running} | thinking={thinking} | typing={typing} | idle={idle}"
    )
    lines.append(f"scan_paths={', '.join(str(p) for p in dashboard.scan_paths)}")
    lines.append(f"log_files={len(dashboard.log_files)} | refresh={dashboard.refresh:.1f}s | max_lines={dashboard.max_lines}")
    lines.append("")

    headers = ["Session", "Process", "State", "Status", "Tool", "Runtime", "LastEvt", "Project"]
    widths = [18, 14, 10, 13, 18, 9, 9, 30]

    head = " ".join(clip(h, widths[i]).ljust(widths[i]) for i, h in enumerate(headers))
    lines.append(head)
    lines.append("-" * len(head))

    if not rows:
        lines.append("No agent sessions detected yet. Keep the dashboard running while Codex is active.")
        return "\n".join(lines)

    for sess in rows:
        process = f"{sess.process_name}:{sess.pid}" if sess.pid else "-"
        runtime = format_age(now - sess.started_at if sess.started_at else None)
        last_evt = format_age(now - sess.last_event_at if sess.last_event_at else None)

        cols = [
            sess.session_id,
            process,
            sess.state,
            sess.status,
            sess.tool,
            runtime,
            last_evt,
            sess.project,
        ]

        row = " ".join(clip(str(col), widths[i]).ljust(widths[i]) for i, col in enumerate(cols))
        lines.append(row)

    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Detailed live dashboard for Codex/Claude agent activity.")
    parser.add_argument("--scan-path", action="append", default=[], help="Path to scan for log files. Can be repeated.")
    parser.add_argument("--refresh", type=float, default=1.0, help="Refresh interval in seconds.")
    parser.add_argument("--max-lines", type=int, default=2000, help="Max lines to ingest per log update.")
    parser.add_argument("--stale-seconds", type=int, default=1800, help="Seconds before stale sessions are pruned.")
    parser.add_argument("--once", action="store_true", help="Render one snapshot and exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    scan_paths = [Path(p).expanduser() for p in args.scan_path] if args.scan_path else default_scan_paths()
    if not scan_paths:
        print("No scan paths found. Provide at least one with --scan-path.")
        return 1

    dashboard = AgentDashboard(
        scan_paths=scan_paths,
        refresh=args.refresh,
        max_lines=max(200, args.max_lines),
        stale_seconds=max(60, args.stale_seconds),
    )

    if args.once:
        dashboard.refresh_once()
        print(render_dashboard(dashboard))
        return 0

    try:
        while True:
            dashboard.refresh_once()
            clear_screen()
            print(render_dashboard(dashboard))
            time.sleep(dashboard.refresh)
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
