import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import api
from local_runtime import local_beta


class LocalBetaRuntimeTests(unittest.TestCase):
    def test_api_status_endpoint_returns_beta_status_payload(self):
        payload = {
            "root": "/tmp/beta",
            "runs": 1,
            "readable_runs": 0,
            "unreadable_runs": 1,
            "latest_run": "",
            "latest_passed": 0,
            "latest_failed": 0,
            "latest_failed_case_ids": [],
            "latest_error": "bad json",
        }

        with patch("api.local_beta.status", return_value=payload):
            response = TestClient(api.app).get(
                "/local/beta/status",
                headers={"Authorization": f"Bearer {api._API_TOKEN}"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "status": payload})

    def test_status_skips_corrupt_latest_run(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            older = runs_dir / "beta_20260426_010000.json"
            newer = runs_dir / "beta_20260426_020000.json"
            older.write_text(
                json.dumps(
                    {
                        "passed": 3,
                        "failed": 1,
                        "failed_case_ids": ["beta_case"],
                    }
                ),
                encoding="utf-8",
            )
            newer.write_text("{not valid json", encoding="utf-8")

            with patch("local_runtime.local_beta.RUNS_DIR", runs_dir), \
                 patch("local_runtime.local_beta.ROOT", runs_dir.parent):
                status = local_beta.status()

        self.assertEqual(status["runs"], 2)
        self.assertEqual(status["readable_runs"], 1)
        self.assertEqual(status["unreadable_runs"], 1)
        self.assertEqual(status["latest_run"], str(older))
        self.assertEqual(status["latest_passed"], 3)
        self.assertEqual(status["latest_failed"], 1)
        self.assertEqual(status["latest_failed_case_ids"], ["beta_case"])
        self.assertEqual(status["latest_error"], "")

    def test_status_reports_error_when_no_runs_are_readable(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            corrupt = runs_dir / "beta_20260426_020000.json"
            corrupt.write_text("{not valid json", encoding="utf-8")

            with patch("local_runtime.local_beta.RUNS_DIR", runs_dir), \
                 patch("local_runtime.local_beta.ROOT", runs_dir.parent):
                status = local_beta.status()

        self.assertEqual(status["runs"], 1)
        self.assertEqual(status["readable_runs"], 0)
        self.assertEqual(status["unreadable_runs"], 1)
        self.assertEqual(status["latest_run"], "")
        self.assertEqual(status["latest_passed"], 0)
        self.assertEqual(status["latest_failed"], 0)
        self.assertTrue(status["latest_error"])

    def test_status_counts_corrupt_older_runs_when_latest_is_readable(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            older = runs_dir / "beta_20260426_010000.json"
            newer = runs_dir / "beta_20260426_020000.json"
            older.write_text("{not valid json", encoding="utf-8")
            newer.write_text(
                json.dumps(
                    {
                        "passed": 4,
                        "failed": 0,
                        "failed_case_ids": [],
                    }
                ),
                encoding="utf-8",
            )

            with patch("local_runtime.local_beta.RUNS_DIR", runs_dir), \
                 patch("local_runtime.local_beta.ROOT", runs_dir.parent):
                status = local_beta.status()

        self.assertEqual(status["runs"], 2)
        self.assertEqual(status["readable_runs"], 1)
        self.assertEqual(status["unreadable_runs"], 1)
        self.assertEqual(status["latest_run"], str(newer))
        self.assertEqual(status["latest_passed"], 4)
        self.assertEqual(status["latest_failed"], 0)
        self.assertEqual(status["latest_error"], "")

    def test_status_surfaces_error_from_latest_readable_run(self):
        with TemporaryDirectory() as tmp:
            runs_dir = Path(tmp)
            latest = runs_dir / "beta_20260426_020000.json"
            latest.write_text(
                json.dumps(
                    {
                        "passed": 0,
                        "failed": 0,
                        "failed_case_ids": [],
                        "error": "timeout",
                    }
                ),
                encoding="utf-8",
            )

            with patch("local_runtime.local_beta.RUNS_DIR", runs_dir), \
                 patch("local_runtime.local_beta.ROOT", runs_dir.parent):
                status = local_beta.status()

        self.assertEqual(status["latest_run"], str(latest))
        self.assertEqual(status["latest_error"], "timeout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
