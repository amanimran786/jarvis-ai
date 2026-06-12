"""Tests for the shared tamper-evident hash-chain helper (infra/_hashchain.py)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from infra import _hashchain as hc


class HashChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "chain.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_append_links_to_genesis_then_prev(self):
        r1 = hc.append_record(self.path, {"n": 1})
        self.assertEqual(r1["prev_hash"], hc.GENESIS)
        r2 = hc.append_record(self.path, {"n": 2})
        self.assertEqual(r2["prev_hash"], r1["this_hash"])

    def test_clean_chain_verifies(self):
        for i in range(5):
            hc.append_record(self.path, {"n": i})
        ok, reason, idx = hc.verify_chain(hc.read_chain(self.path))
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_edit_breaks_chain(self):
        hc.append_record(self.path, {"n": 1})
        hc.append_record(self.path, {"n": 2})
        entries = hc.read_chain(self.path)
        entries[0]["n"] = 999  # forge a past record
        self.path.write_text("\n".join(hc.canonical(e) for e in entries) + "\n")
        ok, reason, idx = hc.verify_chain(hc.read_chain(self.path))
        self.assertFalse(ok)
        self.assertEqual(idx, 0)

    def test_corrupt_line_detected(self):
        hc.append_record(self.path, {"n": 1})
        with open(self.path, "a") as f:
            f.write("{not json}\n")
        ok, reason, idx = hc.verify_chain(hc.read_chain(self.path))
        self.assertFalse(ok)
        self.assertIn("unparseable", reason)

    def test_empty_chain_is_ok(self):
        ok, reason, idx = hc.verify_chain(hc.read_chain(self.path))
        self.assertTrue(ok)

    def test_format_matches_pipeline_audit(self):
        # Cross-check: the helper's output verifies under pipeline_audit's wrapper.
        from infra import pipeline_audit as pa
        hc.append_record(self.path, {"run_id": "x", "v": 1})
        findings = pa.verify_ledger_chain(pa.read_ledger(self.path))
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
