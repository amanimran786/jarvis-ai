"""
Unified security audit stream (infra/security_audit.py).

Invariants under test:
  - every entry lands as one parseable JSONL line with the full schema
  - audit_event never raises, even with an unwritable path
  - filters and summarize() aggregate correctly
  - concurrent writers do not interleave or drop lines
  - plain-language summaries render for every known action
"""
import json
import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from infra import security_audit as sa


class _AuditTmpDir(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.audit_path = Path(self._tmp.name) / "audit.jsonl"
        self._env = patch.dict(
            os.environ, {"JARVIS_SECURITY_AUDIT_PATH": str(self.audit_path)}
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class AuditWriteTests(_AuditTmpDir):
    def test_entry_roundtrip_with_full_schema(self):
        entry = sa.audit_event(
            sa.APPROVAL_DENIED,
            actor="aman",
            target="task deploy build",
            decision="denied",
            severity="warning",
            reason="not reviewed",
            task_id="t42",
            rollback_ref="branch codex/t42",
        )
        self.assertIsNotNone(entry)
        lines = self.audit_path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        on_disk = json.loads(lines[0])
        for field in ("ts", "action", "actor", "target", "decision",
                      "severity", "reason", "task_id", "rollback_ref", "summary"):
            self.assertIn(field, on_disk)
        self.assertEqual(on_disk["action"], sa.APPROVAL_DENIED)
        self.assertEqual(on_disk["task_id"], "t42")
        self.assertIn("denied", on_disk["summary"])

    def test_invalid_severity_normalized_to_info(self):
        entry = sa.audit_event(sa.VAULT_WRITE, severity="catastrophic")
        self.assertEqual(entry["severity"], "info")

    def test_extra_fields_cannot_clobber_schema(self):
        entry = sa.audit_event(
            sa.CLOUD_CALL, actor="security_reviewer",
            extra={"provider": "gemini", "action": "spoofed", "ts": "spoofed"},
        )
        self.assertEqual(entry["action"], sa.CLOUD_CALL)
        self.assertNotEqual(entry["ts"], "spoofed")
        self.assertEqual(entry["extra"]["provider"], "gemini")

    def test_never_raises_on_unwritable_path(self):
        with patch.dict(os.environ, {"JARVIS_SECURITY_AUDIT_PATH": "/dev/null/nope/audit.jsonl"}):
            result = sa.audit_event(sa.RBAC_DENY, actor="agent-x")
        self.assertIsNone(result)  # failed, silently — caller unaffected

    def test_concurrent_writers_do_not_drop_or_interleave(self):
        def write_batch(worker: int):
            for i in range(20):
                sa.audit_event(sa.SECURITY_TOOL_EXEC, actor=f"w{worker}", target=f"run{i}")

        threads = [threading.Thread(target=write_batch, args=(w,)) for w in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        lines = self.audit_path.read_text().splitlines()
        self.assertEqual(len(lines), 100)
        for line in lines:
            json.loads(line)  # every line parseable → no interleaving


class AuditReadTests(_AuditTmpDir):
    def _seed(self):
        sa.audit_event(sa.APPROVAL_GRANTED, actor="aman", target="task a", task_id="t1")
        sa.audit_event(sa.APPROVAL_DENIED, actor="aman", target="task b", task_id="t2",
                       severity="warning")
        sa.audit_event(sa.THREAT_SCREEN_BLOCK, target="task c", task_id="t3",
                       severity="critical")
        sa.audit_event(sa.CLOUD_CALL, actor="security_reviewer", target="verdict",
                       rollback_ref="n/a")

    def test_read_newest_first_and_limit(self):
        self._seed()
        events = sa.read_events(limit=2)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["action"], sa.CLOUD_CALL)

    def test_filters(self):
        self._seed()
        self.assertEqual(len(sa.read_events(action=sa.APPROVAL_DENIED)), 1)
        self.assertEqual(len(sa.read_events(severity="critical")), 1)
        self.assertEqual(sa.read_events(task_id="t1")[0]["action"], sa.APPROVAL_GRANTED)

    def test_corrupt_lines_skipped(self):
        self._seed()
        with open(self.audit_path, "a") as f:
            f.write("{not json}\n")
        events = sa.read_events()
        self.assertEqual(len(events), 4)

    def test_missing_file_returns_empty(self):
        self.assertEqual(sa.read_events(), [])

    def test_summarize_counts_and_plain_language(self):
        self._seed()
        summary = sa.summarize()
        self.assertEqual(summary["total"], 4)
        self.assertEqual(summary["denials"], 1)
        self.assertEqual(summary["threat_blocks"], 1)
        self.assertEqual(summary["cloud_calls"], 1)
        self.assertEqual(summary["by_severity"]["critical"], 1)
        self.assertEqual(len(summary["recent_critical"]), 1)
        self.assertEqual(len(summary["plain_language"]), 4)
        for line in summary["plain_language"]:
            self.assertTrue(line and isinstance(line, str))


class HumanLineTests(unittest.TestCase):
    def test_every_known_action_renders(self):
        for action in (sa.APPROVAL_GRANTED, sa.APPROVAL_DENIED, sa.APPROVAL_REQUESTED,
                       sa.THREAT_SCREEN_BLOCK, sa.SECURITY_VERDICT, sa.CLOUD_CALL,
                       sa.SECURITY_TOOL_EXEC, sa.RBAC_DENY, sa.VAULT_WRITE, sa.ROLLBACK):
            line = sa.human_line({"action": action, "actor": "x", "target": "y",
                                  "decision": "PASS", "reason": "r"})
            self.assertTrue(line)
            self.assertNotIn("{", line)  # no unfilled template slots

    def test_unknown_action_and_garbage_never_raise(self):
        self.assertTrue(sa.human_line({"action": "mystery"}))
        self.assertTrue(sa.human_line({}))
        self.assertTrue(sa.human_line({"action": None, "reason": object()}))


if __name__ == "__main__":
    unittest.main()
