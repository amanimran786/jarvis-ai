import os
import json
import socket
import subprocess
import time
import urllib.error
import urllib.request
import unittest
from pathlib import Path
from contextlib import contextmanager

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
PACKAGED_APP = Path(
    os.getenv(
        "JARVIS_PACKAGED_APP",
        "/Users/truthseeker/Applications/Jarvis.app/Contents/MacOS/Jarvis",
    )
)
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
        "Set JARVIS_RUN_PACKAGED_SMOKE=1 and ensure the installed Jarvis app exists to run the packaged-app smoke test.",
    )(fn)


def _reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def packaged_app_process():
    port = str(_reserve_local_port())
    env = os.environ.copy()
    env.update(
        {
            "JARVIS_API_HOST": "127.0.0.1",
            "JARVIS_API_PORT": port,
            "JARVIS_API_TOKEN": "jarvis-packaged-smoke-token",
        }
    )
    proc = subprocess.Popen(
        [str(PACKAGED_APP), "--no-ui"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        yield proc, port
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def wait_for_packaged_json(
    path: str,
    *,
    port: str,
    headers: dict | None = None,
    payload: dict | None = None,
    deadline_seconds: int = 45,
    request_timeout: float = 1.5,
    proc=None,
):
    url = f"http://127.0.0.1:{port}{path}"
    deadline = time.time() + deadline_seconds
    last_error = ""
    while time.time() < deadline:
        if proc is not None and proc.poll() is not None:
            stdout, stderr = proc.communicate(timeout=1)
            raise AssertionError(
                f"Packaged app exited before serving {path}.\n"
                f"returncode={proc.returncode}\n"
                f"stdout={stdout}\n"
                f"stderr={stderr}"
            )
        try:
            body = None if payload is None else json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(url, data=body, headers=headers or {}, method="GET" if payload is None else "POST")
            if payload is not None:
                request.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            time.sleep(0.5)
    raise AssertionError(f"Packaged app did not serve {path} within the timeout window. Last error: {last_error}")


class LiveApiReadOnlyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(api.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    @live_only
    def test_status_usage_and_cost_policy_endpoints(self):
        status = self.client.get("/status")
        usage = self.client.get("/usage")
        policy = self.client.get("/cost-policy")
        context_budget = self.client.get("/context-budget")
        agent_patterns = self.client.get("/agent-patterns")
        parity = self.client.get("/capability-parity")
        capability_evals = self.client.get("/capability-evals")
        security_roe = self.client.get("/security-roe")
        self.assertEqual(status.status_code, 200)
        self.assertEqual(usage.status_code, 200)
        self.assertEqual(policy.status_code, 200)
        self.assertEqual(context_budget.status_code, 200)
        self.assertEqual(agent_patterns.status_code, 200)
        self.assertEqual(parity.status_code, 200)
        self.assertEqual(capability_evals.status_code, 200)
        self.assertEqual(security_roe.status_code, 200)
        self.assertIn("status", status.json())
        self.assertIn("usage", usage.json())
        self.assertIn("policy", policy.json())
        self.assertIn("profiles", context_budget.json())
        self.assertIn("patterns", agent_patterns.json())
        self.assertIn("features", parity.json())
        self.assertIn("cases", capability_evals.json())
        self.assertIn("templates", security_roe.json())

    @live_only
    def test_router_cost_policy_status_prompt(self):
        stream, label = route_stream("cost policy status")
        text = "".join(stream)
        self.assertEqual(label, "Status")
        self.assertIn("cost policy", text.lower())

    @live_only
    def test_router_context_budget_prompt(self):
        stream, label = route_stream("token optimizer context budget")
        text = "".join(stream)
        self.assertEqual(label, "Status")
        self.assertIn("context budget", text.lower())

    @live_only
    def test_router_expert_prompt_smoke(self):
        stream, label = route_stream(
            "My FastAPI app returns 502 behind Nginx in Docker. Give me the most likely causes and a concrete debugging sequence."
        )
        text = "".join(stream)
        self.assertEqual(label, "Specialized Agents")
        self.assertIn("Specialized agents used", text)

    @live_only
    def test_router_operator_read_only_shell_smoke(self):
        stream, label = route_stream("Use the operator to run command: pwd")
        text = "".join(stream)
        self.assertEqual(label, "Specialized Agents")
        self.assertTrue(text.strip())
        self.assertTrue("/Users/" in text or "/private/" in text)

    @live_only
    def test_router_vault_curator_read_smoke(self):
        stream, label = route_stream("Use the vault curator to read [[80 Jarvis Roadmap]].")
        text = "".join(stream)
        self.assertEqual(label, "Specialized Agents")
        self.assertIn("Jarvis Roadmap", text)


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
        with packaged_app_process() as (proc, port):
            payload = wait_for_packaged_json("/status", port=port, proc=proc)
            self.assertEqual(payload["status"], "online")
            self.assertEqual(payload["api_host"], "127.0.0.1")
            self.assertTrue(payload.get("api_urls"))

    @packaged_smoke_only
    def test_packaged_app_chat_serves_vault_curator_read(self):
        headers = {"Authorization": "Bearer jarvis-packaged-smoke-token"}
        payload = {
            "message": "Use the vault curator to read [[80 Jarvis Roadmap]].",
            "stream": False,
            "source": "packaged_smoke",
        }
        with packaged_app_process() as (proc, port):
            wait_for_packaged_json("/status", port=port, proc=proc)
            response = wait_for_packaged_json(
                "/chat",
                port=port,
                headers=headers,
                payload=payload,
                request_timeout=15.0,
                proc=proc,
            )
            self.assertEqual(response["model"], "Specialized Agents")
            self.assertIn("Jarvis Roadmap", response["response"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
