import subprocess

from scripts import ollama_context_setup as setup


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=["launchctl"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_dry_run_prints_commands_without_launchctl_setenv(capsys):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        assert command[:2] == ["launchctl", "getenv"]
        return _completed(stdout="")

    result = setup.main([], environ={"OLLAMA_CONTEXT_LENGTH": "4096"}, runner=fake_run)

    out = capsys.readouterr().out
    assert result == 0
    assert "Dry run: no launchctl values changed." in out
    assert "OLLAMA_CONTEXT_LENGTH=64000" in out
    assert "env: 4096" in out
    assert "launchctl setenv OLLAMA_CONTEXT_LENGTH 64000" in out
    assert calls
    assert all(call[0][:2] == ["launchctl", "getenv"] for call in calls)


def test_apply_is_required_before_launchctl_setenv(capsys):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[:2] == ["launchctl", "getenv"]:
            return _completed(stdout="existing")
        if command[:2] == ["launchctl", "setenv"]:
            return _completed()
        raise AssertionError(f"unexpected command: {command}")

    setup.main(["--apply"], environ={}, runner=fake_run)

    capsys.readouterr()
    setenv_calls = [call for call in calls if call[0][:2] == ["launchctl", "setenv"]]
    assert [call[0] for call in setenv_calls] == setup.build_launchctl_commands()
    assert all(call[1] == {"check": True} for call in setenv_calls)


def test_reports_launchctl_errors_without_failing(capsys):
    def fake_run(command, **kwargs):
        assert command[:2] == ["launchctl", "getenv"]
        return _completed(stderr="domain not found", returncode=113)

    setup.main([], environ={}, runner=fake_run)

    out = capsys.readouterr().out
    assert "launchctl: unset (domain not found)" in out
    assert "Dry run: no launchctl values changed." in out


def test_export_writes_commands_without_setenv(tmp_path, capsys):
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        assert command[:2] == ["launchctl", "getenv"]
        return _completed(stdout="")

    export_path = tmp_path / "ollama-context.sh"

    setup.main(["--export", str(export_path)], environ={}, runner=fake_run)

    out = capsys.readouterr().out
    exported = export_path.read_text(encoding="utf-8")
    assert f"Exported commands to {export_path}" in out
    assert "launchctl setenv OLLAMA_KV_CACHE_TYPE q8_0" in exported
    assert "launchctl setenv OLLAMA_MAX_LOADED_MODELS 1" in exported
    assert all(command[:2] == ["launchctl", "getenv"] for command in calls)
