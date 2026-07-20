from types import SimpleNamespace
from unittest.mock import call, patch

import kill_and_start


def test_process_match_requires_current_user_and_exact_dashboard_path():
    command = f"501 python {kill_and_start.ROOT / 'jarvis_dashboard.py'}"
    with patch("kill_and_start.os.getuid", return_value=501), \
         patch(
             "kill_and_start.subprocess.run",
             return_value=SimpleNamespace(stdout=command),
         ):
        assert kill_and_start._is_expected_dashboard_process(123) is True


def test_kill_dashboard_port_ignores_unrelated_listener():
    listener = SimpleNamespace(stdout="123\n")
    with patch("kill_and_start.subprocess.run", return_value=listener), \
         patch("kill_and_start._is_expected_dashboard_process", return_value=False), \
         patch("kill_and_start.os.kill") as kill_mock:
        kill_and_start._kill_dashboard_port()

    kill_mock.assert_not_called()


def test_kill_dashboard_port_uses_term_before_kill_escalation():
    listener = SimpleNamespace(stdout="123\n")
    with patch("kill_and_start.subprocess.run", return_value=listener), \
         patch("kill_and_start._is_expected_dashboard_process", return_value=True), \
         patch("kill_and_start._pid_exists", return_value=True), \
         patch("kill_and_start.time.sleep"), \
         patch("kill_and_start.os.kill") as kill_mock:
        kill_and_start._kill_dashboard_port()

    assert kill_mock.call_args_list == [
        call(123, kill_and_start.signal.SIGTERM),
        call(123, kill_and_start.signal.SIGKILL),
    ]
