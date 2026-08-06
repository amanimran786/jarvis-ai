# Jarvis launchd Setup

Keeps `harness/cowork_launcher.py` running under launchd. The supervised process
runs one orchestration iteration immediately and repeats every 300 seconds.

## Install

```bash
/opt/anaconda3/bin/python3 scripts/install_launchd.py --service loop
```

Verify it's running:

```bash
launchctl print gui/$(id -u)/com.jarvis.loop
```

## Uninstall

```bash
/opt/anaconda3/bin/python3 scripts/install_launchd.py --service loop --uninstall
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

- `RunAtLoad = true` and `KeepAlive = true` make launchd start the daemon at
  login and restart it after a crash or kill.
- The daemon owns the 300-second cadence. The plist does not use
  `StartInterval`, which avoids launchd starting overlapping scheduler runs.
- The script is idempotent: safe to call when nothing is pending
- To force a one-off run:
  `/opt/anaconda3/bin/python3 /Users/truthseeker/jarvis-ai/harness/cowork_launcher.py`
- The installer creates the logs and LaunchAgents directories as needed.
- The installed plist is rendered with the current checkout path and the Python
  used to run the installer. Use `--python /path/to/python3` to select another
  Python 3.10+ environment explicitly.

## Restart Verification

```bash
service="gui/$(id -u)/com.jarvis.loop"
old_pid=$(launchctl print "$service" | awk '/pid =/{print $3; exit}')
test -n "$old_pid" || { echo "loop service has no running PID"; exit 1; }
kill "$old_pid"
for _ in $(seq 1 30); do
  new_pid=$(launchctl print "$service" | awk '/pid =/{print $3; exit}')
  test -n "$new_pid" && test "$new_pid" != "$old_pid" && break
  sleep 1
done
test -n "$new_pid" && test "$new_pid" != "$old_pid"
```

The final command exits `0` only when launchd started a replacement process.
