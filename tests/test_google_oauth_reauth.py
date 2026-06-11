import json
from pathlib import Path
from unittest import mock

import google_services


class DummyCreds:
    def __init__(self, payload: dict | None = None, account: str = "jarvis@example.com"):
        self._payload = payload or {"token": "new-token"}
        self.account = account

    def to_json(self) -> str:
        return json.dumps(self._payload)


def test_reauthorize_google_clears_stale_token_and_writes_new_one(tmp_path, monkeypatch):
    credentials_file = tmp_path / "credentials.json"
    token_file = tmp_path / "token.json"
    credentials_file.write_text('{"installed": {"client_id": "x"}}', encoding="utf-8")
    token_file.write_text('{"token": "stale"}', encoding="utf-8")

    monkeypatch.setattr(google_services, "CREDENTIALS_FILE", str(credentials_file))
    monkeypatch.setattr(google_services, "TOKEN_FILE", str(token_file))
    monkeypatch.setattr(google_services, "_ensure_auth_files", lambda: None)

    creds = DummyCreds({"token": "fresh", "refresh_token": "rtok"})
    flow = mock.Mock()
    flow.run_local_server.return_value = creds

    with mock.patch.object(google_services.InstalledAppFlow, "from_client_secrets_file", return_value=flow) as flow_factory:
        result = google_services.reauthorize_google()

    assert result is creds
    flow_factory.assert_called_once_with(str(credentials_file), google_services.SCOPES)
    flow.run_local_server.assert_called_once_with(port=0)
    assert json.loads(token_file.read_text(encoding="utf-8"))["token"] == "fresh"


def test_main_reauth_invokes_reauthorize(capsys):
    creds = DummyCreds()
    with mock.patch.object(google_services, "reauthorize_google", return_value=creds) as reauth:
        exit_code = google_services.main(["--reauth"])

    assert exit_code == 0
    reauth.assert_called_once_with()
    assert "Google OAuth re-authorized" in capsys.readouterr().out


def test_main_status_invokes_get_creds(capsys):
    creds = DummyCreds()
    with mock.patch.object(google_services, "_get_creds", return_value=creds) as get_creds:
        exit_code = google_services.main(["--status"])

    assert exit_code == 0
    get_creds.assert_called_once_with()
    assert "Google OAuth is available" in capsys.readouterr().out


def test_main_without_args_prints_help(capsys):
    exit_code = google_services.main([])

    assert exit_code == 1
    assert "Jarvis Google OAuth helper" in capsys.readouterr().out
