import os
import json
import subprocess
import time
import urllib.error
import urllib.request
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

import api
import browser
import google_services as gs
import messages
from router import route_stream


LIVE = os.getenv("JARVIS_RUN_LIVE_INTEGRATION_TESTS") == "1"
ALLOW_SIDE_EFFECTS = os.getenv("JARVIS_ALLOW_SIDE_EFFECTS") == "1"
TEST_IMESSAGE_RECIPIENT = os.getenv("JARVIS_TEST_IMESSAGE_RECIPIENT", "").strip()
TEST_IMESSAGE_BODY = os.getenv("JARVIS_TEST_IMESSAGE_BODY", "Jarvis live integration test.").strip()
RUN_PACKAGED_SMOKE = os.getenv("JARVIS_RUN_PACKAGED_SMOKE") == "1"
PACKAGED_APP = Path("/Users/truthseeker/jarvis-ai/dist/Jarvis.app/Contents/MacOS/Jarvis")
PACKAGED_SMOKE_PORT = os.getenv("JARVIS_PACKAGED_SMOKE_PORT", "9876")


def live_only(fn):
    return unittest.skipUnless(LIVE, "Set JARVIS_RUN_LIVE_INTEGRATION_TESTS=1 to run live integration tests.")(fn)


def side_effect_only(fn):
    return unittest.skipUnless(
        LIVE and ALLOW_SIDE_EFFECTS,
        "Set JARVIS_RUN_LIVE_INTEGRATION_TESTS=1 and JARVIS_ALLOW_SIDE_EFFECTS=1 to run side-effecting live tests.",
    )(fn)


def packaged_smoke_only(fn):
    return unittest.skipUnless(
        RUN_PACKAGED_SMOKE and PACKAGED_APP.is_file(),
        "Set JARVIS_RUN_PACKAGED_SMOKE=1 and build dist/Jarvis.app first to run the packaged-app smoke test.",
    )(fn)


class LiveApiReadOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(api.app)

    @live_only
    def test_status_usage_and_cost_policy_endpoints(self):
        status = self.client.get("/status")
        usage = self.client.get("/usage")
        policy = self.client.get("/cost-policy")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(usage.status_code, 200)
        self.assertEqual(policy.status_code, 200)
        self.assertIn("status", status.json())
        self.assertIn("usage", usage.json())
        self.assertIn("policy", policy.json())

    @live_only
    def test_router_cost_policy_status_prompt(self):
        stream, label = route_stream("cost policy status")
        text = "".join(stream)
        self.assertEqual(label, "Status")
        self.assertIn("cost policy", text.lower())

    @live_only
    def test_router_expert_prompt_smoke(self):
        stream, label = route_stream(
            "My FastAPI app returns 502 behind Nginx in Docker. Give me the most likely causes and a concrete debugging sequence."
        )
        text = "".join(stream)
        self.assertEqual(label, "Specialized Agents")
        self.assertIn("Specialized agents used", text)


class LiveGoogleReadOnlyTests(unittest.TestCase):
    @live_only
    def test_calendar_read_smoke(self):
        text = gs.get_todays_events()
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())

    @live_only
    def test_gmail_read_smoke(self):
        text = gs.get_unread_emails(max_results=3)
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())


class LiveBrowserReadOnlyTests(unittest.TestCase):
    @live_only
    def test_browser_current_page_smoke(self):
        text = browser.get_current_page()
        self.assertIsInstance(text, str)
        self.assertTrue(text.strip())


class LiveSideEffectTests(unittest.TestCase):
    @side_effect_only
    def test_imessage_send_smoke(self):
        if not TEST_IMESSAGE_RECIPIENT:
            self.skipTest("Set JARVIS_TEST_IMESSAGE_RECIPIENT to run live iMessage send test.")
        text = messages.send_imessage(TEST_IMESSAGE_RECIPIENT, TEST_IMESSAGE_BODY)
        self.assertIn("Sent to", text)


class PackagedAppSmokeTests(unittest.TestCase):
    @packaged_smoke_only
    def test_packaged_app_starts_and_serves_status(self):
        env = os.environ.copy()
        env.update(
            {
                "JARVIS_API_HOST": "127.0.0.1",
                "JARVIS_API_PORT": PACKAGED_SMOKE_PORT,
            }
        )
        proc = subprocess.Popen(
            [str(PACKAGED_APP), "--no-ui"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        payload = None
        try:
            deadline = time.time() + 45
            url = f"http://127.0.0.1:{PACKAGED_SMOKE_PORT}/status"
            while time.time() < deadline:
                if proc.poll() is not None:
                    stdout, stderr = proc.communicate(timeout=1)
                    self.fail(
                        "Packaged app exited before serving /status.\n"
                        f"returncode={proc.returncode}\n"
                        f"stdout={stdout}\n"
                        f"stderr={stderr}"
                    )
                try:
                    with urllib.request.urlopen(url, timeout=1.5) as response:
                        payload = json.load(response)
                    break
                except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError):
                    time.sleep(0.5)

            self.assertIsNotNone(payload, "Packaged app did not serve /status within the timeout window.")
            self.assertEqual(payload["status"], "online")
            self.assertEqual(payload["api_host"], "127.0.0.1")
            self.assertTrue(payload.get("api_urls"))
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
