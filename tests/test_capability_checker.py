from __future__ import annotations

import logging
import os
import subprocess
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest

from harness.capability_checker import check_capability, check_contract_capabilities
from harness.task_contract import Capability, TaskContract, TaskType


def _contract(*capabilities: Capability, working_directory: str = "/repo") -> TaskContract:
    return TaskContract(
        task_id="capability-test",
        task_type=TaskType.ANALYSIS,
        description="Test capability discovery",
        requires_capabilities=list(capabilities),
        working_directory=working_directory,
    )


@pytest.mark.parametrize(
    ("capability", "url", "method"),
    [
        (Capability.OLLAMA, "http://localhost:11434/api/tags", "GET"),
        (Capability.INTERNET, "https://api.openai.com", "HEAD"),
    ],
)
def test_http_capability_uses_assigned_request_and_timeout(
    capability, url, method
):
    response = MagicMock()
    response.__enter__.return_value = response

    with patch(
        "harness.capability_checker.urllib.request.urlopen", return_value=response
    ) as urlopen:
        assert check_capability(capability) is True

    request = urlopen.call_args.args[0]
    assert request.full_url == url
    assert request.get_method() == method
    assert urlopen.call_args.kwargs == {"timeout": 3}


@pytest.mark.parametrize("capability", [Capability.OLLAMA, Capability.INTERNET])
def test_http_capability_returns_false_on_network_failure(capability):
    with patch(
        "harness.capability_checker.urllib.request.urlopen",
        side_effect=OSError("offline"),
    ):
        assert check_capability(capability) is False


def test_internet_capability_accepts_http_error_as_connectivity() -> None:
    response_error = urllib.error.HTTPError(
        "https://api.openai.com",
        421,
        "Misdirected Request",
        hdrs=None,
        fp=None,
    )
    with patch(
        "harness.capability_checker.urllib.request.urlopen",
        side_effect=response_error,
    ):
        assert check_capability(Capability.INTERNET) is True


def test_ollama_capability_rejects_http_error() -> None:
    response_error = urllib.error.HTTPError(
        "http://localhost:11434/api/tags",
        500,
        "Server Error",
        hdrs=None,
        fp=None,
    )
    with patch(
        "harness.capability_checker.urllib.request.urlopen",
        side_effect=response_error,
    ):
        assert check_capability(Capability.OLLAMA) is False


def test_filesystem_capability_requires_read_and_write_access():
    with patch("harness.capability_checker.os.getcwd", return_value="/repo"), patch(
        "harness.capability_checker.os.access", side_effect=[True, False]
    ) as access:
        assert check_capability(Capability.FILESYSTEM) is False

    assert access.call_args_list[0].args == ("/repo", os.R_OK)
    assert access.call_args_list[1].args == ("/repo", os.W_OK)


@pytest.mark.parametrize(
    ("capability", "args"),
    [
        (Capability.GIT, ["git", "--version"]),
        (Capability.PYTHON, ["python3", "--version"]),
    ],
)
def test_subprocess_capability_uses_list_args_shell_false_and_timeout(capability, args):
    completed = subprocess.CompletedProcess(args, 0)
    with patch("harness.capability_checker.subprocess.run", return_value=completed) as run:
        assert check_capability(capability) is True

    run.assert_called_once_with(args, shell=False, timeout=5)


@pytest.mark.parametrize(
    "failure", [subprocess.TimeoutExpired(["git"], 5), OSError("missing")]
)
def test_subprocess_capability_returns_false_on_probe_failure(failure):
    with patch("harness.capability_checker.subprocess.run", side_effect=failure):
        assert check_capability(Capability.GIT) is False


def test_subprocess_capability_returns_false_on_nonzero_exit():
    completed = subprocess.CompletedProcess(["git", "--version"], 1)
    with patch("harness.capability_checker.subprocess.run", return_value=completed):
        assert check_capability(Capability.GIT) is False


def test_voice_capability_queries_devices_when_sounddevice_is_available():
    sounddevice = MagicMock()
    with patch.dict(sys.modules, {"sounddevice": sounddevice}):
        assert check_capability(Capability.VOICE) is True

    sounddevice.query_devices.assert_called_once_with()


def test_voice_capability_returns_false_when_sounddevice_is_missing():
    with patch.dict(sys.modules, {"sounddevice": None}):
        assert check_capability(Capability.VOICE) is False


def test_voice_capability_returns_false_when_device_query_fails():
    sounddevice = MagicMock()
    sounddevice.query_devices.side_effect = RuntimeError("PortAudio unavailable")
    with patch.dict(sys.modules, {"sounddevice": sounddevice}):
        assert check_capability(Capability.VOICE) is False


@pytest.mark.parametrize(
    "capability",
    [Capability.CALENDAR, Capability.IMESSAGE, Capability.SCREEN],
)
def test_unprobeable_capability_logs_note_and_returns_false(capability, caplog):
    with caplog.at_level(logging.INFO, logger="harness.capability_checker"):
        assert check_capability(capability) is False

    assert f"Capability {capability.value} is not yet probeable" in caplog.text


def test_contract_capabilities_use_contract_working_directory_and_names():
    contract = _contract(
        Capability.FILESYSTEM,
        Capability.GIT,
        working_directory="/contract/repo",
    )
    with patch("harness.capability_checker.os.access", return_value=True) as access, patch(
        "harness.capability_checker.check_capability", return_value=False
    ) as check:
        results = check_contract_capabilities(contract)

    assert results == {"filesystem": True, "git": False}
    assert access.call_args_list[0].args == ("/contract/repo", os.R_OK)
    assert access.call_args_list[1].args == ("/contract/repo", os.W_OK)
    check.assert_called_once_with(Capability.GIT)


def test_contract_capability_probe_exception_is_non_blocking():
    contract = _contract(Capability.INTERNET)
    with patch(
        "harness.capability_checker.check_capability",
        side_effect=RuntimeError("probe failed"),
    ):
        assert check_contract_capabilities(contract) == {"internet": False}
