import os
import unittest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
import api


class TestRemoteControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Enforce an active token for testing
        api._API_TOKEN = "jarvis-test-remote-token"
        cls.client = TestClient(api.app)

    def test_unauthorized_endpoints_return_401(self):
        # 1. Telemetry
        resp = self.client.get("/remote/telemetry")
        self.assertEqual(resp.status_code, 401)

        # 2. Screenshot
        resp = self.client.get("/remote/screenshot")
        self.assertEqual(resp.status_code, 401)

        # 3. Volume
        resp = self.client.post("/remote/volume", json={"level": 50})
        self.assertEqual(resp.status_code, 401)

        # 4. Brightness
        resp = self.client.post("/remote/brightness", json={"level": 50})
        self.assertEqual(resp.status_code, 401)

        # 5. Action
        resp = self.client.post("/remote/action", json={"action": "lock"})
        self.assertEqual(resp.status_code, 401)

    def test_authorized_telemetry(self):
        headers = {"Authorization": "Bearer jarvis-test-remote-token"}
        resp = self.client.get("/remote/telemetry", headers=headers)
        self.assertEqual(resp.status_code, 200)
        
        data = resp.json()
        self.assertTrue(data["ok"])
        self.assertIn("telemetry", data)
        tel = data["telemetry"]
        self.assertIn("battery", tel)
        self.assertIn("cpu", tel)
        self.assertIn("memory", tel)
        self.assertIn("os", tel)

    @patch("desktop.screen_capture.capture_screenshot_temp")
    def test_authorized_screenshot(self, mock_capture):
        # Mock capture screenshot to write to a dummy temporary file
        import tempfile
        fd, dummy_path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, 'wb') as tmp:
            tmp.write(b"fake jpeg data")
        
        mock_capture.return_value = dummy_path

        headers = {"Authorization": "Bearer jarvis-test-remote-token"}
        resp = self.client.get("/remote/screenshot", headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers["content-type"], "image/jpeg")
        self.assertEqual(resp.content, b"fake jpeg data")

        # The temp file should have been deleted by the clean generator
        self.assertFalse(os.path.exists(dummy_path))

    @patch("desktop.screen_capture.capture_screenshot_temp")
    def test_query_parameter_authorization_limited_to_media_gets(self, mock_capture):
        # Query tokens are only allowed for browser image/frame GETs.
        import tempfile
        fd, dummy_path = tempfile.mkstemp(suffix=".jpg")
        with os.fdopen(fd, 'wb') as tmp:
            tmp.write(b"fake jpeg data")

        mock_capture.return_value = dummy_path

        resp = self.client.get("/remote/screenshot?token=jarvis-test-remote-token")
        self.assertEqual(resp.status_code, 200)

        resp = self.client.get("/remote/telemetry?token=jarvis-test-remote-token")
        self.assertEqual(resp.status_code, 401)

        resp = self.client.post("/remote/action?token=jarvis-test-remote-token", json={"action": "lock"})
        self.assertEqual(resp.status_code, 401)

    @patch("tools.set_volume")
    def test_authorized_volume(self, mock_set_volume):
        mock_set_volume.return_value = "Volume set to 40."
        headers = {"Authorization": "Bearer jarvis-test-remote-token"}
        resp = self.client.post("/remote/volume", json={"level": 40}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        mock_set_volume.assert_called_once_with(40)

    @patch("tools.set_brightness")
    def test_authorized_brightness(self, mock_set_brightness):
        mock_set_brightness.return_value = "Brightness set to 80%."
        headers = {"Authorization": "Bearer jarvis-test-remote-token"}
        resp = self.client.post("/remote/brightness", json={"level": 80}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        mock_set_brightness.assert_called_once_with(80)

    @patch("tools.lock_screen")
    def test_authorized_action_lock(self, mock_lock):
        mock_lock.return_value = "Locking screen."
        headers = {"Authorization": "Bearer jarvis-test-remote-token"}
        resp = self.client.post("/remote/action", json={"action": "lock"}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])
        mock_lock.assert_called_once()

    @patch("subprocess.run")
    def test_remote_type_escapes_quotes_and_backslashes_in_order(self, mock_run):
        """Regression: escaping must be backslash-first, then quotes, so a body
        containing both stays balanced in the AppleScript `keystroke` literal
        (previously quotes were escaped first, re-doubling the backslash and
        leaving an unescaped quote that could terminate the string early)."""
        mock_run.return_value = MagicMock(returncode=0)
        headers = {"Authorization": "Bearer jarvis-test-remote-token"}
        resp = self.client.post(
            "/remote/type",
            json={"text": 'He said "hi" \\ bye', "submit": False},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["ok"])

        script_call = mock_run.call_args_list[0]
        script_args = script_call.args[0]
        script = script_args[script_args.index("-e") + 1]
        self.assertIn('keystroke "He said \\"hi\\" \\\\ bye"', script)


if __name__ == "__main__":
    unittest.main()
