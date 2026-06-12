"""
Vault writes must be audited and rollbackable.

Every canonical vault mutation through vault_edit emits a VAULT_WRITE entry
to the security audit stream, and its rollback_ref points at a real pre-write
backup file that restores the note byte-for-byte.
"""
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import vault
import vault_edit


_NOTE = """---
title: Test Note
updated: 2026-06-01
version: 1
---

# Test Note

## Incoming

- existing item
"""


class VaultWriteAuditTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.note = self.root / "Test Note.md"
        self.note.write_text(_NOTE, encoding="utf-8")
        self.audit_path = self.root / "audit.jsonl"

        self._patches = [
            patch.dict(os.environ, {"JARVIS_SECURITY_AUDIT_PATH": str(self.audit_path)}),
            patch.object(vault, "VAULT_ROOT", self.root),
            patch.object(vault, "refresh_index", lambda: None),
            patch.object(vault_edit, "_resolve_note_match",
                         lambda ref: {"ok": True, "path": self.note}),
            patch("vault_edit.Path.home", return_value=self.root),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def _audit_entries(self):
        if not self.audit_path.exists():
            return []
        return [json.loads(line) for line in
                self.audit_path.read_text().splitlines() if line.strip()]

    def test_append_emits_audit_entry_with_actionable_rollback(self):
        original = self.note.read_text(encoding="utf-8")

        result = vault_edit.append_under_heading("Test Note", "Incoming", "- new item")

        self.assertTrue(result["ok"])
        self.assertIn("- new item", self.note.read_text(encoding="utf-8"))

        entries = self._audit_entries()
        self.assertEqual(len(entries), 1)
        entry = entries[0]
        self.assertEqual(entry["action"], "vault_write")
        self.assertEqual(entry["decision"], "appended")
        self.assertEqual(entry["target"], str(self.note))

        # rollback_ref is a real file that restores the pre-write content
        rollback = Path(entry["rollback_ref"])
        self.assertTrue(rollback.exists())
        self.assertEqual(rollback.read_text(encoding="utf-8"), original)
        self.assertEqual(result["rollback_ref"], entry["rollback_ref"])

    def test_rollback_restores_note_exactly(self):
        original = self.note.read_text(encoding="utf-8")
        result = vault_edit.append_under_heading("Test Note", "Incoming", "- mistake")

        # the documented rollback procedure: copy backup over the note
        backup = Path(result["rollback_ref"])
        self.note.write_text(backup.read_text(encoding="utf-8"), encoding="utf-8")

        self.assertEqual(self.note.read_text(encoding="utf-8"), original)

    def test_backup_failure_does_not_block_write_or_audit(self):
        with patch.object(vault_edit, "_backup_for_rollback", lambda path, raw: ""):
            result = vault_edit.append_under_heading("Test Note", "Incoming", "- item")
        self.assertTrue(result["ok"])
        entries = self._audit_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["rollback_ref"], "")

    def test_policy_refusals_do_not_emit_or_backup(self):
        self.note.write_text(_NOTE.replace("version: 1", "version: 1\nwrite_policy: propose_only"),
                             encoding="utf-8")
        result = vault_edit.append_under_heading("Test Note", "Incoming", "- item")
        self.assertFalse(result["ok"])
        self.assertEqual(self._audit_entries(), [])


if __name__ == "__main__":
    unittest.main()
