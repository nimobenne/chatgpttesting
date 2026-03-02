<<<<<<< ours
<<<<<<< ours
# Claude Agent Monitor TUI

A live terminal dashboard that monitors active Claude Code agents by:

- Scanning Claude debug logs (including `/.cladude.debug` typo variants)
- Extracting session IDs, project paths, state, and active tool
- Correlating sessions to running `claude` processes
- Displaying runtime and status in a live updating Rich TUI

## Requirements

- Python 3.9+
- `rich`

Install dependency:

```bash
pip install rich
```

## Run

```bash
python claude_agents_dashboard.py
```

Optional flags:

```bash
python claude_agents_dashboard.py --refresh 0.5 --scan-root ~/.claude --scan-root /.cladude.debug
```

Columns shown:

- Session ID
- Project
- State (`typing`, `reading`, `idle`, `sleep`, `waiting`, `unknown`)
- Tool
- PID
- Running time
=======
=======
>>>>>>> theirs
# Claude Agents Monitor (TUI)

A live terminal dashboard that monitors active Claude Code agents by scanning Claude debug logs and matching them to currently running Claude processes. This version is VSCode-friendly and does not require Rich.

## Features

- No external Python dependencies (uses only the standard library + terminal curses support).
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
- Live-updating terminal UI (`q` to quit).

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

Stop with `q` or `Ctrl+C`.


## Notes

- Uses only Python standard library modules (`curses`, `subprocess`, etc.).
- If launched from a non-interactive terminal (like some CI runners), it prints a one-time snapshot instead of opening curses.
<<<<<<< ours
>>>>>>> theirs
=======
>>>>>>> theirs
