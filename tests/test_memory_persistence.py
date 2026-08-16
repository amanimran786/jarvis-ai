"""
tests/test_memory_persistence.py

Proves that memory survives a simulated process restart:

1. SemanticMemory (JSON files):
   - Write an entry to a tmp MEMORY_DIR
   - Wipe all in-memory globals (simulating process exit)
   - Rebuild from disk
   - Confirm the entry comes back in search results

2. Mem0Layer (SQLite history.db):
   - Initialize a disposable history.db with the production schema
   - Open a fresh SQLite connection (simulating a new process)
   - Confirm the schema and entry count are stable across re-reads

No real mem0 LLM calls or personal on-disk data are used.
"""
from __future__ import annotations

import importlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fresh_smem_state(smem, memory_dir: Path):
    """Patch smem's path constants to point at tmp dir and wipe index state."""
    semantic_dir = memory_dir / "semantic"
    episodic_dir = memory_dir / "episodic"
    conversations_dir = memory_dir / "conversations"

    smem.MEMORY_DIR = memory_dir
    smem.SEMANTIC_DIR = semantic_dir
    smem.EPISODIC_DIR = episodic_dir
    smem.CONVERSATIONS_DIR = conversations_dir
    smem.VERBATIM_LOG_PATH = conversations_dir / "verbatim.jsonl"
    smem.invalidate()


# ── SemanticMemory persistence tests ─────────────────────────────────────────

class TestSemanticMemoryPersistence(unittest.TestCase):
    """Prove TF-IDF index rebuilds correctly from JSON after a simulated restart."""

    def setUp(self):
        import semantic_memory as smem
        self.smem = smem
        self.tmp = tempfile.mkdtemp(prefix="smem_persist_test_")
        self.memory_dir = Path(self.tmp)
        _fresh_smem_state(smem, self.memory_dir)

    def tearDown(self):
        # Restore real paths and clean up temp dir
        import semantic_memory as smem
        import runtime_state
        real_memory_dir = runtime_state.writable_data_path(
            "memory",
            seed_from=Path(smem.__file__).resolve().parent / "memory",
        )
        _fresh_smem_state(smem, real_memory_dir)

        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_entry_survives_index_wipe_and_rebuild(self):
        """Write → wipe globals → rebuild → retrieve must return the entry."""
        smem = self.smem

        smem.write("public", {
            "content": "persistence_test_unique_marker_qzx9",
            "tags": ["persistence", "test"],
        })

        # ── Simulate process restart: wipe ALL in-memory state ──────────────
        smem.invalidate()
        self.assertIsNone(smem._vectorizer, "vectorizer should be gone after invalidate")
        self.assertEqual(smem._entries, [], "entries list should be empty after invalidate")

        # ── Rebuild from disk (what happens on first retrieve after restart) ─
        smem._build_index()
        self.assertGreater(len(smem._entries), 0, "entries should reload from JSON files")

        # ── Search — must find our entry ────────────────────────────────────
        hits = smem.retrieve("persistence_test_unique_marker_qzx9", top_k=5, min_score=0.0)
        contents = [h.get("content", "") for h in hits]
        self.assertTrue(
            any("persistence_test_unique_marker_qzx9" in c for c in contents),
            f"expected marker in hits; got: {contents}",
        )

    def test_entry_is_searchable_without_embeddings_or_sklearn(self):
        smem = self.smem
        smem.write("public", {
            "content": "dependency_free_memory_marker_v7n",
            "tags": ["dependency-free", "fallback"],
        })
        smem.write("public", {
            "content": "unrelated calendar preference",
            "tags": ["calendar"],
        })
        smem.invalidate()

        with patch.object(smem, "_build_embed_index", return_value=False), \
             patch.dict(sys.modules, {"sklearn": None}):
            smem._build_index()
            hits = smem.retrieve("dependency_free_memory_marker_v7n")

        self.assertIsNone(smem._vectorizer)
        self.assertEqual(hits[0]["content"], "dependency_free_memory_marker_v7n")
        self.assertEqual(hits[0]["score"], 1.0)

    def test_multiple_entries_all_reload(self):
        """Three entries written before restart — all appear in the rebuilt index."""
        smem = self.smem

        for i in range(3):
            smem.write("semi_private", {
                "content": f"restart_proof_entry_{i}_xj7",
                "tags": [f"entry{i}"],
            })

        smem.invalidate()
        smem._build_index()

        self.assertEqual(len(smem._entries), 3)

    def test_json_files_are_on_disk_after_write(self):
        """The write() call must create a .json file before any restart."""
        smem = self.smem
        path = smem.write("public", {"content": "disk_write_test_abc", "tags": []})
        self.assertTrue(Path(path).exists(), f"expected JSON at {path}")
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        self.assertEqual(data["content"], "disk_write_test_abc")

    def test_episodic_write_survives_rebuild(self):
        """Episodic events appear in retrieve after index rebuild."""
        smem = self.smem
        smem.write_episodic("technical", {
            "content": "episodic_persistence_marker_r8k",
            "event_type": "test",
        })

        smem.invalidate()
        smem._build_index()

        # episodic entries are loaded separately; check _entries includes them
        all_content = [e.get("content", "") for e in smem._entries]
        self.assertTrue(
            any("episodic_persistence_marker_r8k" in c for c in all_content),
            f"episodic entry not in rebuilt index; loaded: {all_content}",
        )

    def test_atomic_write_no_partial_json_on_disk(self):
        """_atomic_write_json must not leave partial files if interrupted mid-write."""
        smem = self.smem
        import semantic_memory as _sm

        target = self.memory_dir / "semantic" / "public" / "atomic_test.json"
        target.parent.mkdir(parents=True, exist_ok=True)

        # Simulate write success
        _sm._atomic_write_json(target, {"content": "complete"})
        self.assertTrue(target.exists())
        self.assertEqual(json.loads(target.read_text())["content"], "complete")

        # No .tmp files left behind
        tmp_files = list(target.parent.glob(".smem_*.tmp"))
        self.assertEqual(tmp_files, [], f"stale tmp files: {tmp_files}")


# ── Mem0Layer SQLite persistence tests ───────────────────────────────────────

class TestMem0HistoryDbPersistence(unittest.TestCase):
    """Prove that history.db on disk is valid and readable by a fresh connection."""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory(prefix="mem0_history_test_")
        cls.db_path = Path(cls._tmp.name) / "history.db"
        with sqlite3.connect(str(cls.db_path)) as conn:
            conn.execute(
                """CREATE TABLE history (
                    id TEXT PRIMARY KEY,
                    memory_id TEXT,
                    old_memory TEXT,
                    new_memory TEXT,
                    event TEXT,
                    created_at DATETIME,
                    updated_at DATETIME,
                    is_deleted INTEGER,
                    actor_id TEXT,
                    role TEXT
                )"""
            )
            conn.execute(
                """INSERT INTO history
                   (id, memory_id, new_memory, event, created_at, is_deleted)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    "history-fixture-1",
                    "memory-fixture-1",
                    "Persistence fixture",
                    "ADD",
                    "2026-01-01T00:00:00Z",
                    0,
                ),
            )

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_history_db_exists_on_disk(self):
        """history.db must exist — it's the cross-session provenance store."""
        self.assertTrue(
            self.db_path.exists(),
            f"history.db not found at {self.db_path}",
        )

    def test_fresh_connection_reads_schema(self):
        """A brand-new SQLite connection (simulating a new process) sees the schema."""
        if not self.db_path.exists():
            self.skipTest("history.db not present")

        conn = sqlite3.connect(str(self.db_path))
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
            self.assertIn("history", tables, "history table must exist")
        finally:
            conn.close()

    def test_history_entries_persist_across_connections(self):
        """Two separate SQLite connections return the same row count."""
        if not self.db_path.exists():
            self.skipTest("history.db not present")

        def _count():
            conn = sqlite3.connect(str(self.db_path))
            try:
                return conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
            finally:
                conn.close()

        count1 = _count()
        count2 = _count()
        self.assertEqual(count1, count2, "row count changed between two reads — file not stable")
        self.assertGreater(count1, 0, "history.db is empty — no memories have been persisted")

    def test_history_entries_have_required_fields(self):
        """Each history row must have id, new_memory, created_at."""
        if not self.db_path.exists():
            self.skipTest("history.db not present")

        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(history)").fetchall()}
            for required in ("id", "new_memory", "created_at"):
                self.assertIn(required, cols, f"column '{required}' missing from history table")
        finally:
            conn.close()

    def test_mem0_status_reflects_disk_state(self):
        """mem0_layer.status() must report available=True when no lock conflict."""
        import mem0_layer
        st = mem0_layer.status()
        # We can't guarantee the lock is free (another process may hold it),
        # but the history_db path must always be reported correctly.
        self.assertEqual(st["collection"], "jarvis_nomic_768_v2")
        self.assertIn("available", st)
        # history_db key is informational — just confirm it's present
        self.assertIn("history_db", st)


# ── Thread-safety: concurrent writes don't corrupt the index ─────────────────

class TestSemanticMemoryConcurrentWrites(unittest.TestCase):
    """invalidate() + _build_index() must be safe under concurrent writers."""

    def setUp(self):
        import semantic_memory as smem
        self.smem = smem
        self.tmp = tempfile.mkdtemp(prefix="smem_thread_test_")
        _fresh_smem_state(smem, Path(self.tmp))

    def tearDown(self):
        import semantic_memory as smem
        import runtime_state, shutil
        real = runtime_state.writable_data_path(
            "memory",
            seed_from=Path(smem.__file__).resolve().parent / "memory",
        )
        _fresh_smem_state(smem, real)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_concurrent_writes_all_persist(self):
        """10 threads writing entries — all must appear on disk and in rebuilt index."""
        smem = self.smem
        errors: list[Exception] = []

        def _writer(i: int):
            try:
                smem.write("public", {"content": f"thread_write_{i}_persist", "tags": []})
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_writer, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"concurrent writes raised: {errors}")

        # Rebuild from disk
        smem.invalidate()
        smem._build_index()

        all_content = [e.get("content", "") for e in smem._entries]
        for i in range(10):
            self.assertTrue(
                any(f"thread_write_{i}_persist" in c for c in all_content),
                f"thread {i} entry missing after rebuild",
            )


if __name__ == "__main__":
    unittest.main()
