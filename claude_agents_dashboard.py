#!/usr/bin/env python3
"""Live terminal dashboard for monitoring active Claude Code agents."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from rich import box
from rich.console import Console
from rich.live import Live
from rich.table import Table


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
    source_file: str
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
    parser.add_argument(
        "--scan-path",
        action="append",
        default=[],
        help="Path to scan for debug logs (can be repeated).",
    )
    parser.add_argument("--refresh", type=float, default=1.0, help="Refresh interval in seconds (default: 1.0).")
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
        with path.open("rb") as f:
            data = f.read()
    except (OSError, UnicodeDecodeError):
        return []

    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return []

    lines = text.splitlines()
    if len(lines) <= max_lines:
        return lines
    return lines[-max_lines:]


def first_match(pattern: re.Pattern[str], lines: Iterable[str], reverse: bool = False) -> Optional[str]:
    sequence = list(lines)
    if reverse:
        sequence = list(reversed(sequence))
    for line in sequence:
        m = pattern.search(line)
        if m:
            return m.group(1).strip()
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
                normalized = raw.replace("Z", "+00:00")
                return dt.datetime.fromisoformat(normalized).timestamp()
            except ValueError:
                continue
    return None


def running_claude_pids() -> Dict[int, str]:
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.SubprocessError:
        return {}

    result: Dict[int, str] = {}
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(maxsplit=1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        args = parts[1] if len(parts) > 1 else ""
        if "claude" in args.lower():
            result[pid] = args
    return result


def parse_agent_from_log(path: Path, max_lines: int, live_pids: Dict[int, str]) -> Optional[AgentSnapshot]:
    lines = tail_lines(path, max_lines=max_lines)
    if not lines:
        return None

    session_id = first_match(SESSION_PATTERN, lines, reverse=True) or path.stem
    pid_text = first_match(PID_PATTERN, lines, reverse=True)
    pid = int(pid_text) if pid_text and pid_text.isdigit() else None

    project = first_match(PROJECT_PATTERN, lines, reverse=True)
    if not project and pid and pid in live_pids:
        cmd = live_pids[pid]
        m = re.search(r"(?:--cwd|--project)\s+([^\s]+)", cmd)
        project = m.group(1) if m else "unknown"
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

    return AgentSnapshot(
        session_id=session_id,
        pid=pid,
        project=project,
        state=state,
        tool=tool,
        started_at=started_at,
        source_file=str(path),
        is_running=is_running,
    )


def collect_agents(scan_paths: List[Path], max_lines: int) -> List[AgentSnapshot]:
    live_pids = running_claude_pids()
    logs = gather_log_files(scan_paths)
    snapshots: List[AgentSnapshot] = []
    for log in logs:
        snap = parse_agent_from_log(log, max_lines=max_lines, live_pids=live_pids)
        if snap and (snap.is_running or snap.state != "unknown"):
            snapshots.append(snap)

    unique: Dict[str, AgentSnapshot] = {}
    for snap in snapshots:
        unique[snap.session_id] = snap
    return sorted(unique.values(), key=lambda s: (not s.is_running, s.session_id))


def build_table(snapshots: List[AgentSnapshot], scan_paths: List[Path]) -> Table:
    table = Table(title="Claude Code Agents Monitor", box=box.SIMPLE_HEAVY)
    table.add_column("Session ID", overflow="fold")
    table.add_column("PID", justify="right")
    table.add_column("Project", overflow="fold")
    table.add_column("State")
    table.add_column("Tool")
    table.add_column("Runtime", justify="right")
    table.add_column("Status")

    if not snapshots:
        table.add_row("-", "-", "-", "-", "-", "-", "No agents detected")
    else:
        for snap in snapshots:
            status = "🟢 running" if snap.is_running else "🟡 log-only"
            table.add_row(
                snap.session_id,
                str(snap.pid or "-"),
                snap.project,
                snap.state,
                snap.tool,
                snap.runtime,
                status,
            )

    scan_info = ", ".join(str(p) for p in scan_paths) if scan_paths else "(none found)"
    table.caption = f"Scanning: {scan_info}"
    return table


def main() -> int:
    args = parse_args()
    paths = [Path(p).expanduser() for p in args.scan_path] if args.scan_path else default_scan_paths()

    console = Console()
    if not paths:
        console.print("[yellow]No debug paths found. Use --scan-path to provide one or more log directories.[/yellow]")
        return 1

    with Live(build_table([], paths), console=console, refresh_per_second=max(1, int(1 / max(args.refresh, 0.1)))) as live:
        try:
            while True:
                snapshots = collect_agents(paths, max_lines=args.max_lines)
                live.update(build_table(snapshots, paths))
                time.sleep(args.refresh)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(main())
