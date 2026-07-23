# scripts/

One-time installers and maintenance utilities for jarvis-ai.

| Script | Purpose |
|--------|---------|
| install_launchd.py | Installs/removes the dashboard and orchestrator macOS launchd services |
| setup_plugins.py   | Sets up the CODEX-7 plugin system (base class, loader, examples) |
| git_commit.py      | Commits pending changes with a standard message |

## User-facing CLIs (in root)

- `history_cli.py`   — Browse task history in the terminal
- `plugin_manager.py` — List, enable, disable, test plugins
