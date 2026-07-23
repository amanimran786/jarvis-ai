from __future__ import annotations

import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import orchestrator_loop
from harness import cowork_launcher
from scripts import install_launchd


REPO_ROOT = Path(__file__).resolve().parent.parent
LOOP_PLIST = REPO_ROOT / "scripts" / "com.jarvis.loop.plist"


def test_loop_plist_supervises_conda_daemon_without_start_interval():
    with LOOP_PLIST.open("rb") as handle:
        config = plistlib.load(handle)

    arguments = config["ProgramArguments"]
    assert arguments[:2] == [
        "/opt/anaconda3/bin/python3",
        "/Users/truthseeker/jarvis-ai/harness/cowork_launcher.py",
    ]
    assert arguments[2:] == ["--daemon", "--interval", "300"]
    assert config["RunAtLoad"] is True
    assert config["KeepAlive"] is True
    assert config["ExitTimeOut"] == 120
    assert config["WorkingDirectory"] == "/Users/truthseeker/jarvis-ai"
    assert "StartInterval" not in config


def test_launchd_installer_validates_checked_in_services():
    assert install_launchd._validate_source("dashboard") is None
    assert install_launchd._validate_source("loop") is None


def test_launchd_installer_bootstraps_selected_service(tmp_path, monkeypatch):
    source = tmp_path / "com.jarvis.loop.plist"
    source.write_bytes(LOOP_PLIST.read_bytes())
    launch_agents = tmp_path / "LaunchAgents"
    logs_dir = tmp_path / "logs"
    commands: list[list[str]] = []

    def fake_run(args, timeout=None):
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(
        install_launchd,
        "SERVICES",
        {
            "loop": {
                "label": "com.jarvis.loop",
                "source": source,
                "entry_point": REPO_ROOT / "harness" / "cowork_launcher.py",
            }
        },
    )
    monkeypatch.setattr(install_launchd, "LAUNCH_AGENTS", launch_agents)
    monkeypatch.setattr(install_launchd, "LOGS_DIR", logs_dir)
    monkeypatch.setattr(install_launchd, "_domain", lambda: "gui/501")
    monkeypatch.setattr(install_launchd, "_run", fake_run)
    monkeypatch.setattr(install_launchd, "_service_state", lambda *_: "loaded")
    wait_for_state = Mock(return_value=True)
    monkeypatch.setattr(install_launchd, "_wait_for_state", wait_for_state)

    result = install_launchd.install_service("loop", sys.executable)

    assert result == 0
    with (launch_agents / source.name).open("rb") as handle:
        installed = plistlib.load(handle)
    assert installed["ProgramArguments"][:2] == [
        str(Path(sys.executable).absolute()),
        str((REPO_ROOT / "harness" / "cowork_launcher.py").resolve()),
    ]
    assert ["launchctl", "bootout", "gui/501/com.jarvis.loop"] in commands
    assert [
        "launchctl",
        "bootstrap",
        "gui/501",
        str(launch_agents / source.name),
    ] in commands
    assert wait_for_state.call_count == 2
    assert wait_for_state.call_args_list[0].args == (
        "gui/501",
        "com.jarvis.loop",
        "unloaded",
    )
    assert wait_for_state.call_args_list[1].args == (
        "gui/501",
        "com.jarvis.loop",
        "running",
    )
    assert (
        wait_for_state.call_args_list[1].kwargs["stable_seconds"]
        == install_launchd.RUNNING_STABILITY_SECONDS
    )


def test_launchd_installer_reports_bootstrap_failure(tmp_path, monkeypatch, capsys):
    source = tmp_path / "com.jarvis.loop.plist"
    source.write_bytes(LOOP_PLIST.read_bytes())
    launch_agents = tmp_path / "agents"
    launch_agents.mkdir()
    destination = launch_agents / source.name
    previous = b"previous launchd configuration"
    destination.write_bytes(previous)
    bootstrap_calls = 0

    def fake_run(args, timeout=None):
        nonlocal bootstrap_calls
        if args[1] == "bootstrap":
            bootstrap_calls += 1
        return subprocess.CompletedProcess(
            args,
            1 if args[1] == "bootstrap" and bootstrap_calls == 1 else 0,
            "",
            "bootstrap denied",
        )

    monkeypatch.setattr(
        install_launchd,
        "SERVICES",
        {
            "loop": {
                "label": "com.jarvis.loop",
                "source": source,
                "entry_point": REPO_ROOT / "harness" / "cowork_launcher.py",
            }
        },
    )
    monkeypatch.setattr(install_launchd, "LAUNCH_AGENTS", launch_agents)
    monkeypatch.setattr(install_launchd, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(install_launchd, "_run", fake_run)
    monkeypatch.setattr(install_launchd, "_service_state", lambda *_: "loaded")
    monkeypatch.setattr(install_launchd, "_wait_for_state", Mock(return_value=True))

    result = install_launchd.install_service("loop", sys.executable)

    assert result == 1
    assert "bootstrap denied" in capsys.readouterr().err
    assert destination.read_bytes() == previous
    assert bootstrap_calls == 2


def test_launchd_installer_does_not_rollback_before_service_mutation(
    tmp_path, monkeypatch, capsys
):
    source = tmp_path / "com.jarvis.loop.plist"
    source.write_bytes(LOOP_PLIST.read_bytes())
    launch_agents = tmp_path / "agents"
    launch_agents.mkdir()
    destination = launch_agents / source.name
    previous = b"healthy existing configuration"
    destination.write_bytes(previous)
    commands: list[list[str]] = []

    def fake_run(args, timeout=None):
        commands.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(
        install_launchd,
        "SERVICES",
        {
            "loop": {
                "label": "com.jarvis.loop",
                "source": source,
                "entry_point": REPO_ROOT / "harness" / "cowork_launcher.py",
            }
        },
    )
    monkeypatch.setattr(install_launchd, "LAUNCH_AGENTS", launch_agents)
    monkeypatch.setattr(install_launchd, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(install_launchd, "_run", fake_run)
    monkeypatch.setattr(
        install_launchd,
        "_prepare_logs",
        Mock(side_effect=OSError("log directory unavailable")),
    )

    result = install_launchd.install_service("loop", sys.executable)

    assert result == 1
    assert "log directory unavailable" in capsys.readouterr().err
    assert destination.read_bytes() == previous
    assert not any(command[:2] == ["launchctl", "bootout"] for command in commands)


def test_service_state_does_not_treat_launchctl_errors_as_unloaded(monkeypatch):
    monkeypatch.setattr(
        install_launchd,
        "_run",
        lambda *_: subprocess.CompletedProcess(
            ["launchctl"], 1, "", "Operation not permitted"
        ),
    )

    with pytest.raises(RuntimeError, match="Operation not permitted"):
        install_launchd._service_state("gui/501", "com.jarvis.loop")


def test_loop_skips_embedded_dashboard_when_service_is_already_listening(
    monkeypatch,
):
    socket_connection = Mock()
    socket_connection.__enter__ = Mock(return_value=socket_connection)
    socket_connection.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(
        orchestrator_loop.socket,
        "create_connection",
        Mock(return_value=socket_connection),
    )
    response = Mock(status=401)
    response.read.return_value = b'{"error":"dashboard_auth_required"}'
    http_connection = Mock()
    http_connection.getresponse.return_value = response
    monkeypatch.setattr(
        orchestrator_loop.http.client,
        "HTTPConnection",
        Mock(return_value=http_connection),
    )
    start_thread = Mock(side_effect=AssertionError("must not start another dashboard"))
    monkeypatch.setattr(orchestrator_loop.threading, "Thread", start_thread)
    monkeypatch.setattr(orchestrator_loop, "_DASHBOARD_THREAD", None)

    orchestrator_loop._ensure_dashboard_running(port=7842)

    start_thread.assert_not_called()


def test_run_forever_runs_immediately_and_stops_during_wait(monkeypatch):
    stop_event = Mock()
    stop_event.is_set.return_value = False
    stop_event.wait.return_value = True
    run_once = Mock()
    monkeypatch.setattr(cowork_launcher, "run", run_once)

    cowork_launcher.run_forever(interval_seconds=5, stop_event=stop_event)

    run_once.assert_called_once_with()
    stop_event.wait.assert_called_once_with(5)


def test_run_forever_isolates_iteration_failure(monkeypatch, caplog):
    stop_event = Mock()
    stop_event.is_set.return_value = False
    stop_event.wait.return_value = True
    monkeypatch.setattr(
        cowork_launcher,
        "run",
        Mock(side_effect=RuntimeError("temporary queue failure")),
    )

    cowork_launcher.run_forever(interval_seconds=10, stop_event=stop_event)

    assert "scheduler iteration failed" in caplog.text
    stop_event.wait.assert_called_once_with(10)


def test_run_forever_rejects_non_positive_interval():
    with pytest.raises(ValueError, match="greater than zero"):
        cowork_launcher.run_forever(interval_seconds=0)


def test_cli_preserves_one_shot_default_and_accepts_daemon_interval():
    one_shot = cowork_launcher._parse_args([])
    daemon = cowork_launcher._parse_args(["--daemon", "--interval", "15"])

    assert one_shot.daemon is False
    assert one_shot.interval == cowork_launcher.DEFAULT_INTERVAL_SECONDS
    assert daemon.daemon is True
    assert daemon.interval == 15
