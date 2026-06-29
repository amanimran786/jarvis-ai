# Jarvis launchd Setup

Runs `harness/cowork_launcher.py` every 300 seconds (5 min) as a macOS launchd job.

## Install

```bash
cp scripts/com.jarvis.loop.plist ~/Library/LaunchAgents/com.jarvis.loop.plist
launchctl load ~/Library/LaunchAgents/com.jarvis.loop.plist
```

Verify it's running:

```bash
launchctl list | grep com.jarvis.loop
```

## Uninstall

```bash
launchctl unload ~/Library/LaunchAgents/com.jarvis.loop.plist
rm ~/Library/LaunchAgents/com.jarvis.loop.plist
```

## Logs

| Stream | Path |
|--------|------|
| stdout | `logs/launchd.log` |
| stderr | `logs/launchd_error.log` |

```bash
tail -f /Users/truthseeker/jarvis-ai/logs/launchd.log
tail -f /Users/truthseeker/jarvis-ai/logs/launchd_error.log
```

## Notes

- `RunAtLoad = true` — fires once immediately on `launchctl load`, then every 300s
- The script is idempotent: safe to call when nothing is pending
- To force a one-off run: `python3 /Users/truthseeker/jarvis-ai/harness/cowork_launcher.py`
- Logs directory must exist before loading: `mkdir -p /Users/truthseeker/jarvis-ai/logs`
