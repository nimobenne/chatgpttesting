#!/usr/bin/env python3
"""Live terminal dashboard for monitoring active Claude Code agents without external deps."""

from __future__ import annotations

import argparse
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
