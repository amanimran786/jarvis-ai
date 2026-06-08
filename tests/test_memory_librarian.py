"""
tests/test_memory_librarian.py
==============================

Pytest suite for agents/memory_librarian.py.

All external services are stubbed via sys.modules mock injection so the
suite runs in isolation. Each test patches the lazy-imported module
attributes (store / mem0_layer / semantic_memory / review) inside the
agents.memory_librarian namespace.
"""
import sys
import importlib
from dataclasses import is_dataclass
from unittest.mock import MagicMock, patch

# ── Mock/Stub external dependencies before import ─────────────────────────────

_REAL_MODULES = {
    "config": importlib.import_module("config"),
    "qdrant_client": sys.modules.get("qdrant_client"),
    "qdrant_client.models": sys.modules.get("qdrant_client.models"),
    "brains.brain_ollama": sys.modules.get("brains.brain_ollama"),
}

# Config stub
_mock_config = MagicMock()
_mock_config.LOCAL_DEFAULT = "glm-4.7-flash"
_mock_config.AGENT_ROSTER = {
    "memory_librarian": {
        "role": "Memory librarian. Indexes, retrieves, and curates information in the Jarvis knowledge base.",
        "tools": ["file_read", "file_write", "memory"],
        "model": "glm-4.7-flash",
    }
}
# Track originals for post-test restoration
_STUB_MODS = ["config", "qdrant_client", "qdrant_client.models", "brains.brain_ollama"]

# Qdrant stub
_mock_qdrant = MagicMock()
_mock_models = MagicMock()
_mock_brain = MagicMock()

# Install stubs needed for the module-level imports below
sys.modules["config"] = _mock_config
sys.modules["qdrant_client"] = _mock_qdrant
sys.modules["qdrant_client.models"] = _mock_models
sys.modules["brains.brain_ollama"] = _mock_brain

# Pre-stash the backends we expect memory_librarian to import lazily. The
# module resolves them at top level via try/except, so we don't *have* to
# pre-stub — but doing so keeps tests deterministic regardless of whether
# real backends are installed in the host venv.
for _mod in ("mem0_layer", "semantic_memory"):
    sys.modules.setdefault(_mod, MagicMock())


# ── Module under test ─────────────────────────────────────────────────────────

# Drop only the agents.memory_librarian submodule cache so we get the fresh
# implementation, but leave the `agents` package in place so its submodules
# remain importable.
sys.modules.pop("agents.memory_librarian", None)

import pytest

@pytest.fixture(autouse=True)
def _restore_module_stubs():
    """Keep stubs alive during each test; restore originals afterwards."""
    sys.modules["config"] = _mock_config
    sys.modules["qdrant_client"] = _mock_qdrant
    sys.modules["qdrant_client.models"] = _mock_models
    sys.modules["brains.brain_ollama"] = _mock_brain
    yield
    for k in _STUB_MODS:
        orig = _REAL_MODULES.get(k)
        if orig is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = orig

from agents.memory_librarian import (
    MemoryHit,
    CurationReport,
    ingest,
    recall,
    curate,
    promote,
    vault_propose,
    _normalize_content,
    _VALID_LAYERS,
)

for _name, _module in _REAL_MODULES.items():
    if _module is None:
        sys.modules.pop(_name, None)
    else:
        sys.modules[_name] = _module


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def backends():
    """Patch the four lazy-imported backends inside agents.memory_librarian.

    Returns the four MagicMock objects in order:
        (store, mem0_layer, semantic_memory, review)
    """
    from unittest.mock import patch
    from agents import memory_librarian as ml

    with (
        patch.object(ml, "store", new=MagicMock()) as mock_store,
        patch.object(ml, "mem0_layer", new=MagicMock()) as mock_mem0,
        patch.object(ml, "semantic_memory", new=MagicMock()) as mock_semantic,
        patch.object(ml, "review", new=MagicMock()) as mock_review,
    ):
        yield mock_store, mock_mem0, mock_semantic, mock_review


# ── Data class sanity ────────────────────────────────────────────────────────

def test_dataclasses_exist():
    assert is_dataclass(MemoryHit)
    assert is_dataclass(CurationReport)
    r = CurationReport()
    assert r.pruned == 0
    assert r.deduplicated == 0
    assert r.summarized == 0


def test_valid_layers_constant():
    assert set(_VALID_LAYERS) == {"working", "project", "personal", "knowledge"}


def test_normalize_content_helper():
    assert _normalize_content("Hello   World") == "hello world"
    assert _normalize_content("  Foo\nBAR\t") == "foo bar"
    assert _normalize_content("") == ""


# ── ingest ───────────────────────────────────────────────────────────────────

def test_ingest_success_returns_entry_id(backends):
    mock_store, mock_mem0, _, _ = backends
    mock_store.write.return_value = "vector-entry-1"

    result = ingest(
        content="User prefers dark mode.",
        source="ui_settings",
        agent_id="human",
        session_id="sess-A",
        project_id="proj-1",
        layer="personal",
        key="ui.theme",
        importance=7,
    )

    assert result == "vector-entry-1"
    mock_store.write.assert_called_once()
    kwargs = mock_store.write.call_args.kwargs
    assert kwargs["layer"] == "personal"
    assert kwargs["content"] == "User prefers dark mode."
    assert kwargs["agent_id"] == "human"
    assert kwargs["session_id"] == "sess-A"
    assert kwargs["project_id"] == "proj-1"
    assert kwargs["key"] == "ui.theme"
    assert kwargs["importance"] == 7
    assert kwargs["metadata"] == {"source": "ui_settings"}
    assert kwargs["force"] is False

    mock_mem0.add.assert_called_once()
    mk = mock_mem0.add.call_args.kwargs
    assert mk["text"] == "User prefers dark mode."
    assert mk["metadata"]["source"] == "ui_settings"
    assert mk["metadata"]["layer"] == "personal"


def test_ingest_empty_content_returns_none(backends):
    mock_store, mock_mem0, _, _ = backends
    assert ingest("", "x", "a", "s", "p", "working", "k", 5) is None
    assert ingest("   ", "x", "a", "s", "p", "working", "k", 5) is None
    mock_store.write.assert_not_called()
    mock_mem0.add.assert_not_called()


def test_ingest_invalid_layer_returns_none(backends):
    mock_store, mock_mem0, _, _ = backends
    result = ingest("hi", "x", "a", "s", "p", "garbage_layer", "k", 5)
    assert result is None
    mock_store.write.assert_not_called()
    mock_mem0.add.assert_not_called()


def test_ingest_graceful_degradation_when_store_raises(backends):
    mock_store, mock_mem0, _, _ = backends
    mock_store.write.side_effect = RuntimeError("Ollama down")

    result = ingest("payload", "src", "agent1", "s1", "p1", "working", "k", 5)
    assert result is None


def test_ingest_mem0_failure_does_not_block_primary(backends):
    mock_store, mock_mem0, _, _ = backends
    mock_store.write.return_value = "ok-id"
    mock_mem0.add.side_effect = RuntimeError("mem0 OOM")

    result = ingest("payload", "src", "agent1", "s1", "p1", "working", "k", 5)
    assert result == "ok-id"  # primary still succeeded


def test_ingest_works_when_all_backends_disabled(monkeypatch):
    """If every backend import failed at module load, ingest should still
    return None cleanly rather than raising NameError."""
    from agents import memory_librarian as ml

    with (
        patch.object(ml, "store", None),
        patch.object(ml, "mem0_layer", None),
    ):
        result = ingest("hi", "src", "a", "s", "p", "working", "k", 5)
        assert result is None


# ── recall ───────────────────────────────────────────────────────────────────

def test_recall_fanout_and_deduplication(backends):
    mock_store, mock_mem0, mock_semantic, _ = backends

    # Vector hit
    v_hit = MagicMock()
    v_hit.content = "duplicate memory"
    v_hit.score = 0.95
    v_hit.metadata = {"source": "vector"}
    mock_store.recall.return_value = [v_hit]

    # mem0 hit (DUPLICATE of vector hit)
    mock_mem0.search.return_value = [
        {"memory": "DUPLICATE MEMORY", "score": 0.88, "metadata": {}}
    ]

    # semantic hit (UNIQUE)
    mock_semantic.retrieve.return_value = [
        {"content": "unique semantic fact", "score": 0.75, "_tier": "public"}
    ]

    results = recall("query", "sess1", "proj1", top_k=5, layers=["personal"])

    # Vector hit + semantic hit. mem0 duplicate should be skipped.
    assert len(results) == 2
    contents = [r.content for r in results]
    assert "duplicate memory" in contents
    assert "unique semantic fact" in contents

    # Ranked by score descending
    assert results[0].score == 0.95
    assert results[1].score == 0.75

    # Sources labeled
    sources = {r.source for r in results}
    assert "vector_personal" in sources or "vector" in sources
    assert "semantic" in sources


def test_recall_empty_query_returns_empty(backends):
    results = recall("", "s", "p", 5, ["working"])
    assert results == []


def test_recall_zero_topk_returns_empty(backends):
    results = recall("hi", "s", "p", 0, ["working"])
    assert results == []


def test_recall_falls_back_through_all_backends(backends):
    mock_store, mock_mem0, mock_semantic, _ = backends
    mock_store.recall.return_value = []
    mock_mem0.search.return_value = []
    mock_semantic.retrieve.return_value = []

    results = recall("anything", "s", "p", 5, ["working"])
    assert results == []

    mock_store.recall.assert_called()
    mock_mem0.search.assert_called_once()
    mock_semantic.retrieve.assert_called_once()


def test_recall_topk_truncates_results(backends):
    mock_store, _, _, _ = backends
    hits = []
    for i in range(10):
        h = MagicMock()
        h.content = f"item {i}"
        h.score = 0.9 - i * 0.01
        h.metadata = {"source": f"v{i}"}
        hits.append(h)
    mock_store.recall.return_value = hits

    results = recall("q", "s", "p", top_k=3, layers=["working"])
    assert len(results) == 3
    assert [r.content for r in results] == ["item 0", "item 1", "item 2"]


def test_recall_graceful_degradation(backends):
    """All three backends raise — recall returns [], does not crash."""
    mock_store, mock_mem0, mock_semantic, _ = backends
    mock_store.recall.side_effect = RuntimeError("qdrant down")
    mock_mem0.search.side_effect = RuntimeError("mem0 down")
    mock_semantic.retrieve.side_effect = RuntimeError("semantic down")

    results = recall("q", "s", "p", 5, ["working"])
    assert results == []


def test_recall_handles_legacy_mem0_text_key(backends):
    """Older mem0 builds return 'text' instead of 'memory'."""
    _, mock_mem0, _, _ = backends
    mock_mem0.search.return_value = [{"text": "legacy fact", "score": 0.6}]

    results = recall("q", "s", "p", 5, [])
    assert any(r.content == "legacy fact" and r.source == "mem0" for r in results)


def test_recall_filters_invalid_layers(backends):
    mock_store, _, _, _ = backends
    mock_store.recall.return_value = []

    results = recall("q", "s", "p", 5, layers=["working", "bogus", "knowledge"])
    # Should call store.recall only for the two valid layers
    called_layers = {c.kwargs.get("layer") for c in mock_store.recall.call_args_list}
    assert called_layers == {"working", "knowledge"}


# ── curate ───────────────────────────────────────────────────────────────────

def test_curate_execution(backends):
    mock_store, _, _, _ = backends
    mock_store.prune.return_value = 12
    mock_store.summarize_working.return_value = "Session summarized."

    # Provide a personal layer with two duplicates
    p1 = MagicMock()
    p1.content = "User likes dark mode"
    p1.entry_id = "p1"
    p2 = MagicMock()
    p2.content = "USER LIKES DARK MODE"  # duplicate (normalised)
    p2.entry_id = "p2"
    p3 = MagicMock()
    p3.content = "User likes light mode"
    p3.entry_id = "p3"
    mock_store._scroll_all.return_value = [p1, p2, p3]

    report = curate("sess1")

    assert isinstance(report, CurationReport)
    assert report.pruned == 12
    assert report.summarized == 1
    assert report.deduplicated == 1
    mock_store.forget.assert_called_once_with("personal", entry_id="p2")


def test_curate_no_summary_when_below_threshold(backends):
    mock_store, _, _, _ = backends
    mock_store.prune.return_value = 0
    mock_store.summarize_working.return_value = None  # nothing to summarise
    mock_store._scroll_all.return_value = []

    report = curate("sess1")
    assert report.pruned == 0
    assert report.summarized == 0
    assert report.deduplicated == 0


def test_curate_handles_personal_scroll_failure(backends):
    mock_store, _, _, _ = backends
    mock_store.prune.return_value = 0
    mock_store.summarize_working.return_value = None
    mock_store._scroll_all.side_effect = RuntimeError("qdrant down")

    report = curate("sess1")
    assert report.deduplicated == 0
    # prune + summarize should still have been attempted
    mock_store.prune.assert_called_once()


def test_curate_returns_empty_report_when_store_disabled(monkeypatch):
    from agents import memory_librarian as ml
    with patch.object(ml, "store", None):
        report = curate("sess1")
        assert report == CurationReport(0, 0, 0)


# ── promote ──────────────────────────────────────────────────────────────────

def test_promote_knowledge_security_gate_fail_returns_none(backends):
    _, _, _, mock_review = backends
    mock_review.return_value = "FAIL"

    result = promote("entry_123", "project", "knowledge", "agent_admin")
    assert result is None
    mock_review.assert_called_once()


def test_promote_knowledge_security_gate_pass(backends):
    mock_store, _, _, mock_review = backends
    mock_review.return_value = "PASS"
    mock_store._scroll_all.return_value = []  # source not found in store
    mock_store.write.return_value = "new-kn-id"

    result = promote("entry_123", "project", "knowledge", "agent_admin")
    # Falls back to synthetic id when source is not in the mock store
    assert result == "promoted_entry_123"
    mock_review.assert_called_once()


def test_promote_knowledge_force_bypasses_security_gate(backends):
    mock_store, _, _, mock_review = backends
    # If force=True, even a FAIL should not block the move
    mock_review.return_value = "FAIL"
    mock_store._scroll_all.return_value = []
    mock_store.write.return_value = None  # write returns nothing → synthetic id

    result = promote(
        "entry_123", "project", "knowledge", "agent_admin", force=True
    )
    assert result == "promoted_entry_123"


def test_promote_working_to_project_skips_security(backends):
    """working -> project must not invoke the security reviewer."""
    mock_store, _, _, mock_review = backends
    src = MagicMock()
    src.entry_id = "w1"
    src.content = "working note"
    src.importance = 5
    src.metadata = {}
    mock_store._scroll_all.return_value = [src]
    mock_store.write.return_value = "proj-id"

    result = promote("w1", "working", "project", "agent_x")
    assert result == "proj-id"
    mock_review.assert_not_called()


def test_promote_invalid_args_return_none(backends):
    _, _, _, mock_review = backends
    assert promote("", "working", "project", "a") is None
    assert promote("e", "bogus", "project", "a") is None
    assert promote("e", "working", "bogus", "a") is None
    mock_review.assert_not_called()


def test_promote_writes_to_target_and_forgets_source(backends):
    mock_store, _, _, mock_review = backends
    # Source entry found in the source layer
    src = MagicMock()
    src.entry_id = "src-1"
    src.content = "important knowledge"
    src.importance = 9
    src.metadata = {"key": "k.thing", "extra": "value"}
    mock_store._scroll_all.return_value = [src]
    mock_store.write.return_value = "tgt-1"
    mock_review.return_value = "PASS"

    result = promote("src-1", "project", "knowledge", "agent_x")
    assert result == "tgt-1"

    # Target write called with correct args + provenance metadata
    w = mock_store.write.call_args
    assert w.kwargs["layer"] == "knowledge"
    assert w.kwargs["content"] == "important knowledge"
    assert w.kwargs["importance"] == 9
    assert w.kwargs["metadata"]["promoted_from"] == "project"
    assert w.kwargs["metadata"]["original_entry_id"] == "src-1"

    # Source was forgotten
    mock_store.forget.assert_called_once_with("project", entry_id="src-1")


def test_promote_no_source_found_returns_synthetic_id(backends):
    mock_store, _, _, mock_review = backends
    mock_review.return_value = "PASS"
    mock_store._scroll_all.return_value = []  # source not present

    result = promote("missing-1", "project", "knowledge", "agent_x")
    # No write happened, so we fall back to the synthetic handle
    assert result == "promoted_missing-1"
    mock_store.write.assert_not_called()


# ── vault_propose ────────────────────────────────────────────────────────────

def test_vault_propose_returns_string_no_write():
    proposal = vault_propose(
        "New architecture design",
        "vault/design.md",
        "User requested architecture log",
    )
    assert isinstance(proposal, str)
    assert "VAULT WRITE PROPOSAL" in proposal
    assert "vault/design.md" in proposal
    assert "User requested architecture log" in proposal
    assert "New architecture design" in proposal
    assert "Pending Human Approval" in proposal
    assert "never writes to the vault" in proposal.lower()


def test_vault_propose_handles_empty_path_and_reason():
    proposal = vault_propose("content", "", "")
    assert "<unspecified>" in proposal
    assert "<no reason given>" in proposal
    assert "VAULT WRITE PROPOSAL" in proposal


def test_vault_propose_handles_none_content():
    proposal = vault_propose(None, "vault/x.md", "reason")
    # Should not raise
    assert "Content Length: 0 chars" in proposal
    assert "vault/x.md" in proposal


def test_vault_propose_never_writes():
    """Hard guard: vault_propose must not open, write, or touch any path.

    The function is a pure string formatter. Even if the caller passes a
    real path, no filesystem call should be made.
    """
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "should_not_appear.md"
        proposal = vault_propose("payload", str(target), "test")
        assert not target.exists(), "vault_propose must never write to disk"
        assert str(target) in proposal  # but it does mention the path
