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
