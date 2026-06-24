"""
test_semantic_memory_persistence.py

Proves that semantic_memory entries survive a full process restart.

The test strategy: write entries, wipe ALL in-memory state (exactly what
happens at process exit / startup), then rebuild from disk and assert the
entries come back.  This is equivalent to killing -9 the process after a
write completes and verifying the data in a fresh process.

We also verify atomic-write safety: simulate an interrupted write by leaving
a .smem_*.tmp file behind and confirm the next reload ignores it without
crashing (the real guarantee — no partial JSON is ever left as the final
file).
"""

import json
import os
import tempfile
import threading
import time

import pytest

import semantic_memory as smem
from semantic_memory import (
    SEMANTIC_DIR,
    EPISODIC_DIR,
    CONVERSATIONS_DIR,
    VERBATIM_LOG_PATH,
)


# ── helpers ───────────────────────────────────────────────────────────────────

def _wipe_index():
    """Reset all in-memory index state — mirrors what happens at process start."""
    smem._vectorizer = None
    smem._matrix = None
    smem._entries = []
    smem._embed_vecs = []
    smem._embed_ready = False
    smem._embed_matrix = None
    with smem._embed_cache_lock:
        smem._embed_cache.clear()


def _rebuild_index_no_embed():
    """Rebuild index from disk without calling Ollama — TF-IDF only.

    This simulates a cold-start where the embedding service is not yet
    available.  The important property is that disk entries are loaded.
    """
    _wipe_index()
    entries = smem._load_all_entries()
    smem._entries = entries
    # Try to build TF-IDF (may skip silently if sklearn absent)
    try:
        import contextlib, io
        with contextlib.redirect_stderr(io.StringIO()):
            from sklearn.feature_extraction.text import TfidfVectorizer
        docs = [smem._doc_text(e) for e in entries]
        v = TfidfVectorizer(ngram_range=(1, 2), min_df=1, sublinear_tf=True, strip_accents="unicode")
        with contextlib.redirect_stderr(io.StringIO()):
            smem._matrix = v.fit_transform(docs)
        smem._vectorizer = v
    except Exception:
        pass
    return entries


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def isolated_memory_dir(tmp_path, monkeypatch):
    """Redirect all semantic_memory paths to a temp directory for each test.

    This prevents tests from touching the real memory store and from
    interfering with each other.
    """
    sem_dir = tmp_path / "semantic"
    epi_dir = tmp_path / "episodic"
    conv_dir = tmp_path / "conversations"
    for d in (sem_dir / "public", sem_dir / "semi_private",
              epi_dir / "professional", epi_dir / "technical", conv_dir):
        d.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(smem, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(smem, "SEMANTIC_DIR", sem_dir)
    monkeypatch.setattr(smem, "EPISODIC_DIR", epi_dir)
    monkeypatch.setattr(smem, "CONVERSATIONS_DIR", conv_dir)
    monkeypatch.setattr(smem, "VERBATIM_LOG_PATH", conv_dir / "verbatim.jsonl")

    _wipe_index()
    yield tmp_path
    _wipe_index()


# ── semantic write → disk → cold-reload ──────────────────────────────────────

class TestSemanticWritePersistence:
    def test_semantic_entry_survives_index_wipe(self, isolated_memory_dir):
        """Entry written to disk must be present after full in-memory wipe."""
        path = smem.write("public", {
            "content": "Aman prefers local-first AI tools",
            "tags": ["preference", "ai"],
        })
        assert path.exists(), "write() must create the file on disk"

        # Simulate process restart: wipe all in-memory state
        _wipe_index()
        assert smem._entries == [], "index must be empty after wipe"

        # Cold-reload from disk
        entries = _rebuild_index_no_embed()
        contents = [e.get("content", "") for e in entries]
        assert any("local-first AI tools" in c for c in contents), (
            f"Entry not found after cold reload. Loaded: {contents}"
        )

    def test_semantic_file_is_valid_json_on_disk(self, isolated_memory_dir):
        """Confirm the file written is parseable JSON — not a partial write."""
        path = smem.write("semi_private", {
            "content": "Interview at Google on 2026-07-01",
            "tags": ["interview", "google"],
        })
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)   # raises if partial / corrupt
        assert parsed["content"] == "Interview at Google on 2026-07-01"
        assert parsed["privacy_tier"] == "semi_private"
        assert "id" in parsed
        assert "created_at" in parsed

    def test_multiple_writes_all_survive_reload(self, isolated_memory_dir):
        """Three distinct entries must all be present after a cold reload."""
        entries_to_write = [
            {"content": f"Persistent fact {i}", "tags": [f"tag{i}"]}
            for i in range(3)
        ]
        for e in entries_to_write:
            smem.write("public", e)

        _wipe_index()
        loaded = _rebuild_index_no_embed()
        loaded_contents = {e.get("content", "") for e in loaded}

        for e in entries_to_write:
            assert e["content"] in loaded_contents, (
                f"Missing after reload: {e['content']}"
            )


# ── episodic write → disk → cold-reload ──────────────────────────────────────

class TestEpisodicWritePersistence:
    def test_episodic_entry_survives_index_wipe(self, isolated_memory_dir):
        path = smem.write_episodic("professional", {
            "content": "Passed final round at Anthropic",
            "tags": ["interview", "anthropic"],
        })
        assert path.exists()

        _wipe_index()
        entries = _rebuild_index_no_embed()
        contents = [e.get("content", "") for e in entries]
        assert any("Anthropic" in c for c in contents), (
            f"Episodic entry missing after reload. Loaded: {contents}"
        )

    def test_episodic_file_is_valid_json(self, isolated_memory_dir):
        path = smem.write_episodic("technical", {
            "content": "Upgraded GLM context window to 128K",
            "tags": ["llm", "optimization"],
        })
        parsed = json.loads(path.read_text())
        assert parsed["domain"] == "technical"
        assert "id" in parsed
        assert "timestamp" in parsed


# ── conversation JSONL persistence ────────────────────────────────────────────

class TestConversationLogPersistence:
    def test_conversation_turn_is_appended_to_disk(self, isolated_memory_dir):
        log_path = smem.VERBATIM_LOG_PATH
        assert not log_path.exists() or log_path.stat().st_size == 0

        smem.log_conversation_turn(
            user_input="What models do we have locally?",
            assistant_response="GLM 4.7 Flash, qwen3:8b, jarvis-local.",
            model="glm-4.7-flash",
        )

        assert log_path.exists(), "verbatim.jsonl must be created"
        lines = [l for l in log_path.read_text().splitlines() if l.strip()]
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert "locally" in record["user"]
        assert "GLM" in record["assistant"]

    def test_conversation_survives_index_wipe(self, isolated_memory_dir):
        smem.log_conversation_turn(
            user_input="Remember the M4 Pro has 48 GB RAM",
            assistant_response="Noted. That supports up to 32B models.",
        )

        _wipe_index()
        entries = _rebuild_index_no_embed()
        contents = " ".join(e.get("content", "") for e in entries)
        assert "48 GB RAM" in contents or "M4 Pro" in contents, (
            "Conversation turn not loaded from disk after index wipe"
        )


# ── atomic write safety ───────────────────────────────────────────────────────

class TestAtomicWriteSafety:
    def test_stale_tmp_file_does_not_block_reload(self, isolated_memory_dir):
        """A leftover .smem_*.tmp file (simulating a killed write) must be
        ignored — _load_all_entries only reads *.json, never *.tmp."""
        pub_dir = smem.SEMANTIC_DIR / "public"
        stale_tmp = pub_dir / ".smem_abcdef.tmp"
        stale_tmp.write_text('{"broken": true, "content": "orphaned"')  # partial JSON

        _wipe_index()
        # Must not raise, and the partial .tmp must not appear in results
        entries = _rebuild_index_no_embed()
        contents = [e.get("content", "") for e in entries]
        assert "orphaned" not in contents, (
            "Stale .tmp file should never be loaded as a memory entry"
        )

    def test_corrupt_json_file_is_skipped_not_fatal(self, isolated_memory_dir):
        """A corrupt .json file (disk full / SIGKILL during old write_text call)
        must be skipped — _load_all_entries wraps each file in try/except."""
        pub_dir = smem.SEMANTIC_DIR / "public"

        # Write one valid entry
        smem.write("public", {"content": "valid entry", "tags": []})

        # Inject a corrupt .json file (simulates old non-atomic write + kill)
        (pub_dir / "corrupt_abc123.json").write_text('{"content": "broken"')

        _wipe_index()
        entries = _rebuild_index_no_embed()
        contents = [e.get("content", "") for e in entries]

        assert "valid entry" in contents, "Valid entry must survive alongside corrupt file"
        assert not any("broken" in c for c in contents), (
            "Corrupt entry must be silently dropped"
        )

    def test_atomic_write_leaves_no_tmp_after_success(self, isolated_memory_dir):
        """After a successful write(), no .smem_*.tmp files should remain."""
        pub_dir = smem.SEMANTIC_DIR / "public"
        smem.write("public", {"content": "clean write test", "tags": []})

        tmp_files = list(pub_dir.glob(".smem_*.tmp"))
        assert tmp_files == [], (
            f"Stale tmp files found after successful write: {tmp_files}"
        )


# ── concurrent write safety ───────────────────────────────────────────────────

class TestConcurrentWrites:
    def test_parallel_writes_all_land_on_disk(self, isolated_memory_dir):
        """Four concurrent write() calls must each produce a distinct file."""
        results = []
        errors = []

        def _write(i):
            try:
                p = smem.write("public", {
                    "content": f"Concurrent entry {i}",
                    "tags": [f"t{i}"],
                })
                results.append(p)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_write, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Concurrent writes raised: {errors}"
        assert len(results) == 4
        for p in results:
            assert p.exists(), f"File missing after concurrent write: {p}"

        # All four entries must be loadable
        _wipe_index()
        entries = _rebuild_index_no_embed()
        loaded = {e.get("content", "") for e in entries}
        for i in range(4):
            assert f"Concurrent entry {i}" in loaded, (
                f"Concurrent entry {i} missing after reload"
            )
