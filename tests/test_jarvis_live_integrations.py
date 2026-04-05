import os
import unittest

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


def live_only(fn):
    return unittest.skipUnless(LIVE, "Set JARVIS_RUN_LIVE_INTEGRATION_TESTS=1 to run live integration tests.")(fn)


def side_effect_only(fn):
    return unittest.skipUnless(
        LIVE and ALLOW_SIDE_EFFECTS,
        "Set JARVIS_RUN_LIVE_INTEGRATION_TESTS=1 and JARVIS_ALLOW_SIDE_EFFECTS=1 to run side-effecting live tests.",
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
