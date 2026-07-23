#!/usr/bin/env python3
"""Install or remove Jarvis launchd services from checked-in plists."""

from __future__ import annotations

import argparse
import os
import pathlib
import plistlib
import subprocess
import sys
import time
from typing import Sequence

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"
LAUNCH_AGENTS = pathlib.Path.home() / "Library" / "LaunchAgents"
COMMAND_TIMEOUT_SECONDS = 10
STATE_TIMEOUT_SECONDS = 15
STOP_TIMEOUT_SECONDS = 130
RUNNING_STABILITY_SECONDS = 2
_MISSING_SERVICE_MARKERS = (
    "could not find service",
    "could not find specified service",
    "service not found",
)

SERVICES = {
    "dashboard": {
        "label": "com.jarvis.dashboard",
        "source": REPO_ROOT / "scripts" / "com.jarvis.dashboard.plist",
        "entry_point": REPO_ROOT / "jarvis_dashboard.py",
    },
    "loop": {
        "label": "com.jarvis.loop",
        "source": REPO_ROOT / "scripts" / "com.jarvis.loop.plist",
        "entry_point": REPO_ROOT / "harness" / "cowork_launcher.py",
    },
}


def _run(
    args: Sequence[str],
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[str]:
    command = list(args)
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            command,
            124,
            exc.stdout or "",
            f"command timed out after {timeout:.0f}s",
        )


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _destination(service: str) -> pathlib.Path:
    return LAUNCH_AGENTS / pathlib.Path(SERVICES[service]["source"]).name


def _service_state(domain: str, label: str) -> str:
    result = _run(["launchctl", "print", f"{domain}/{label}"])
    if result.returncode != 0:
        error = f"{result.stdout}\n{result.stderr}".strip().lower()
        if any(marker in error for marker in _MISSING_SERVICE_MARKERS):
            return "unloaded"
        raise RuntimeError(
            f"launchctl could not inspect {domain}/{label}: "
            f"{result.stderr.strip() or f'exit {result.returncode}'}"
        )
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("state ="):
            return stripped.partition("=")[2].strip()
    return "loaded"


def _wait_for_state(
    domain: str,
    label: str,
    expected: str,
    timeout: float = STATE_TIMEOUT_SECONDS,
    stable_seconds: float = 0,
) -> bool:
    deadline = time.monotonic() + timeout
    stable_since: float | None = None
    while time.monotonic() < deadline:
        state = _service_state(domain, label)
        now = time.monotonic()
        if state == expected:
            if stable_seconds <= 0:
                return True
            if stable_since is None:
                stable_since = now
            elif now - stable_since >= stable_seconds:
                return True
        else:
            stable_since = None
        time.sleep(0.25)
    return False


def _validate_source(service: str) -> str | None:
    source = pathlib.Path(SERVICES[service]["source"])
    entry_point = pathlib.Path(SERVICES[service]["entry_point"])
    label = str(SERVICES[service]["label"])
    if not source.is_file():
        return f"source plist not found: {source}"
    if not entry_point.is_file():
        return f"service entry point not found: {entry_point}"
    try:
        with source.open("rb") as handle:
            config = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException) as exc:
        return f"invalid source plist: {exc}"

    if config.get("Label") != label:
        return f"source plist label does not match {label}"
    arguments = config.get("ProgramArguments")
    if not isinstance(arguments, list) or not arguments:
        return "source plist has no ProgramArguments"
    if config.get("KeepAlive") is not True:
        return f"source plist must supervise {label} with KeepAlive"
    if service == "loop" and "--daemon" not in arguments:
        return "loop plist must start cowork_launcher.py in daemon mode"
    return None


def _validate_python(python_executable: str | pathlib.Path) -> tuple[pathlib.Path | None, str | None]:
    executable = pathlib.Path(python_executable).expanduser().absolute()
    if not executable.exists():
        return None, f"Python executable not found: {executable}"
    if not executable.is_file() or not os.access(executable, os.X_OK):
        return None, f"Python is not executable: {executable}"
    result = _run(
        [
            str(executable),
            "-c",
            "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)",
        ]
    )
    if result.returncode != 0:
        return None, f"Python 3.10 or newer is required: {executable}"
    return executable, None


def _write_installed_plist(
    service: str,
    destination: pathlib.Path,
    python_executable: pathlib.Path,
) -> None:
    source = pathlib.Path(SERVICES[service]["source"])
    entry_point = pathlib.Path(SERVICES[service]["entry_point"]).resolve()
    with source.open("rb") as handle:
        config = plistlib.load(handle)

    arguments = list(config["ProgramArguments"])
    arguments[0] = str(python_executable)
    arguments[1] = str(entry_point)
    config["ProgramArguments"] = arguments
    config["WorkingDirectory"] = str(REPO_ROOT.resolve())
    stdout_name = pathlib.Path(config["StandardOutPath"]).name
    stderr_name = pathlib.Path(config["StandardErrorPath"]).name
    config["StandardOutPath"] = str((LOGS_DIR / stdout_name).resolve())
    config["StandardErrorPath"] = str((LOGS_DIR / stderr_name).resolve())

    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            plistlib.dump(config, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _prepare_logs(plist_path: pathlib.Path) -> None:
    with plist_path.open("rb") as handle:
        config = plistlib.load(handle)
    LOGS_DIR.chmod(0o700)
    for key in ("StandardOutPath", "StandardErrorPath"):
        path = pathlib.Path(config[key])
        path.touch(exist_ok=True)
        path.chmod(0o600)


def _atomic_write_bytes(path: pathlib.Path, value: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.restore")
    try:
        with temporary.open("wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _restore_previous_service(
    domain: str,
    label: str,
    destination: pathlib.Path,
    previous_plist: bytes | None,
    was_loaded: bool,
) -> bool:
    """Best-effort rollback after a failed service upgrade."""
    try:
        state = _service_state(domain, label)
        if state != "unloaded":
            _run(
                ["launchctl", "bootout", f"{domain}/{label}"],
                timeout=STOP_TIMEOUT_SECONDS,
            )
            if not _wait_for_state(
                domain, label, "unloaded", timeout=STOP_TIMEOUT_SECONDS
            ):
                return False
        if previous_plist is None:
            try:
                destination.unlink()
            except FileNotFoundError:
                pass
            return not was_loaded

        _atomic_write_bytes(destination, previous_plist)
        if not was_loaded:
            return True
        result = _run(["launchctl", "bootstrap", domain, str(destination)])
        if result.returncode != 0:
            return False
        return _wait_for_state(domain, label, "running")
    except (OSError, RuntimeError):
        return False


def install_service(
    service: str,
    python_executable: str | pathlib.Path = sys.executable,
) -> int:
    validation_error = _validate_source(service)
    if validation_error:
        print(f"ERROR: {validation_error}", file=sys.stderr)
        return 1
    executable, python_error = _validate_python(python_executable)
    if python_error or executable is None:
        print(f"ERROR: {python_error}", file=sys.stderr)
        return 1

    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    domain = _domain()
    label = str(SERVICES[service]["label"])
    destination = _destination(service)
    candidate = destination.with_name(f".{destination.name}.{os.getpid()}.candidate")
    previous_plist: bytes | None = None
    was_loaded = False
    service_mutated = False

    try:
        _write_installed_plist(service, candidate, executable)
        lint = _run(["plutil", "-lint", str(candidate)])
        if lint.returncode != 0:
            print(
                f"ERROR: plist validation failed: {lint.stderr.strip()}",
                file=sys.stderr,
            )
            return 1
        _prepare_logs(candidate)

        previous_plist = destination.read_bytes() if destination.exists() else None
        previous_state = _service_state(domain, label)
        was_loaded = previous_state != "unloaded"

        if was_loaded:
            service_mutated = True
            stopped = _run(
                ["launchctl", "bootout", f"{domain}/{label}"],
                timeout=STOP_TIMEOUT_SECONDS,
            )
            if stopped.returncode != 0:
                raise RuntimeError(
                    f"launchctl bootout failed for {label}: "
                    f"{stopped.stderr.strip() or f'exit {stopped.returncode}'}"
                )
            if not _wait_for_state(
                domain, label, "unloaded", timeout=STOP_TIMEOUT_SECONDS
            ):
                raise RuntimeError(f"timed out stopping {label}")

        service_mutated = True
        os.replace(candidate, destination)
        result = _run(["launchctl", "bootstrap", domain, str(destination)])
        if result.returncode != 0:
            raise RuntimeError(
                f"launchctl bootstrap failed for {label}: "
                f"{result.stderr.strip() or f'exit {result.returncode}'}"
            )
        if not _wait_for_state(
            domain,
            label,
            "running",
            stable_seconds=RUNNING_STABILITY_SECONDS,
        ):
            raise RuntimeError(
                f"{label} did not remain running for "
                f"{RUNNING_STABILITY_SECONDS}s; check logs in {LOGS_DIR}"
            )
    except (OSError, RuntimeError, plistlib.InvalidFileException) as exc:
        restored = (
            _restore_previous_service(
                domain,
                label,
                destination,
                previous_plist,
                was_loaded,
            )
            if service_mutated
            else True
        )
        suffix = "" if restored else " (previous service could not be restored)"
        print(f"ERROR: {exc}{suffix}", file=sys.stderr)
        return 1
    finally:
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass

    print(f"Installed plist: {destination}")
    print(f"SUCCESS: {label} is running and supervised by launchd.")
    return 0


def uninstall_service(service: str) -> int:
    domain = _domain()
    label = str(SERVICES[service]["label"])
    try:
        state = _service_state(domain, label)
        if state != "unloaded":
            result = _run(
                ["launchctl", "bootout", f"{domain}/{label}"],
                timeout=STOP_TIMEOUT_SECONDS,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    result.stderr.strip() or f"exit {result.returncode}"
                )
            if not _wait_for_state(
                domain, label, "unloaded", timeout=STOP_TIMEOUT_SECONDS
            ):
                raise RuntimeError(f"timed out stopping {label}")
    except RuntimeError as exc:
        print(f"ERROR: could not stop {label}: {exc}", file=sys.stderr)
        return 1
    try:
        _destination(service).unlink()
    except FileNotFoundError:
        pass
    print(f"Removed {label}.")
    return 0


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage Jarvis launchd services")
    parser.add_argument(
        "--service",
        choices=(*SERVICES, "all"),
        default="dashboard",
        help="service to manage (default: dashboard)",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="stop the selected service and remove its installed plist",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python 3.10+ interpreter to install in the plist (default: current Python)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    selected = list(SERVICES) if args.service == "all" else [args.service]
    for service in selected:
        result = (
            uninstall_service(service)
            if args.uninstall
            else install_service(service, args.python)
        )
        if result != 0:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
