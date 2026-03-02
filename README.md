# Claude Agents Monitor (TUI)

A live terminal dashboard that monitors active Claude Code agents by scanning Claude debug logs and matching them to currently running Claude processes.

## Features

- Auto-discovers common Claude debug directories:
  - `/.cladude.debug`
  - `/.claude.debug`
  - `~/.cladude.debug`
  - `~/.claude.debug`
  - `~/.config/claude`
- Displays:
  - Session ID
  - PID
  - Project/workspace
  - Current state (`typing`, `reading`, `idle`, `sleep`, `waiting for input`)
  - Active tool (if found)
  - Runtime
  - Running/log-only status
- Live-updating Rich TUI.

## Install

```bash
python3 -m pip install rich
```

## Run

```bash
python3 claude_agents_dashboard.py
```

Custom scan paths:

```bash
python3 claude_agents_dashboard.py --scan-path ~/.claude.debug --scan-path /tmp/claude-logs
```

Adjust refresh interval:

```bash
python3 claude_agents_dashboard.py --refresh 0.5
```

Stop with `Ctrl+C`.
