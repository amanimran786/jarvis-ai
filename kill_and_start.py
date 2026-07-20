#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _is_expected_dashboard_process(pid: int) -> bool:
    result = subprocess.run(
        ["/bin/ps", "-o", "uid=,command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    details = result.stdout.strip()
    if not details:
        return False
    uid_text, _, command = details.partition(" ")
    try:
        owner_uid = int(uid_text)
    except ValueError:
        return False
    dashboard = str((ROOT / "jarvis_dashboard.py").resolve())
    return owner_uid == os.getuid() and dashboard in command.strip()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _kill_dashboard_port() -> None:
    """Stop only this checkout's dashboard process on the dashboard port."""
    result = subprocess.run(
        ["/usr/sbin/lsof", "-nP", "-iTCP:7842", "-sTCP:LISTEN", "-t"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    for raw_pid in result.stdout.splitlines():
        try:
            pid = int(raw_pid.strip())
        except ValueError:
            continue
        if pid == os.getpid() or not _is_expected_dashboard_process(pid):
            continue
        os.kill(pid, signal.SIGTERM)
        for _ in range(10):
            if not _pid_exists(pid):
                break
            time.sleep(0.1)
        else:
            if _is_expected_dashboard_process(pid):
                os.kill(pid, signal.SIGKILL)


def main() -> None:
    _kill_dashboard_port()
    time.sleep(1.5)
    dashboard = ROOT / "jarvis_dashboard.py"
    os.chdir(ROOT)
    os.execv(sys.executable, [sys.executable, str(dashboard)])


if __name__ == "__main__":
    main()
