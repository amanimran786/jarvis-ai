"""
Tests for infra/memory.py — four-layer vector memory system.

All Qdrant and embedding calls are mocked so tests run without any
external services.
"""
import sys
import time
import uuid
from unittest.mock import MagicMock, patch, call

# ── Stub external deps before import ─────────────────────────────────────────

# config stub
_mock_config = MagicMock()
sys.modules["config"] = _mock_config

# qdrant_client stubs
_mock_qdrant_mod    = MagicMock()
_mock_qdrant_models = MagicMock()

# Real dataclasses from qdrant_client.models that the code uses by name
from qdrant_client.models import (
    Distance, VectorParams, PointStruct,
    Filter, FieldCondition, MatchValue,
)
_mock_qdrant_models.Distance     = Distance
_mock_qdrant_models.VectorParams = VectorParams
_mock_qdrant_models.PointStruct  = PointStruct
_mock_qdrant_models.Filter       = Filter
_mock_qdrant_models.FieldCondition = FieldCondition
_mock_qdrant_models.MatchValue   = MatchValue

sys.modules["qdrant_client"]        = _mock_qdrant_mod
sys.modules["qdrant_client.models"] = _mock_qdrant_models

# brain_ollama stub
_mock_brain = MagicMock()
sys.modules["brains.brain_ollama"] = _mock_brain

# Force re-import of the module under test with fresh mocks
if "infra.memory" in sys.modules:
    del sys.modules["infra.memory"]
if "infra" in sys.modules:
    del sys.modules["infra"]

# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_store():
    """Return a MemoryStore with a pre-initialized mock Qdrant client."""
    # Always reload real qdrant classes by clearing any mock first — another
    # test file collected earlier (test_memory_librarian) may have installed a
    # bare MagicMock() without the real classes on it, corrupting the module-
    # level _mock_qdrant_models assignments that happened after that mock was in
    # place. Clearing sys.modules["qdrant_client*"] forces a real import below.
    sys.modules.pop("qdrant_client", None)
    sys.modules.pop("qdrant_client.models", None)
    from qdrant_client.models import (
        Distance, VectorParams, PointStruct,
        Filter, FieldCondition, MatchValue,
    )
    _mock_qdrant_models.Distance       = Distance
    _mock_qdrant_models.VectorParams   = VectorParams
    _mock_qdrant_models.PointStruct    = PointStruct
    _mock_qdrant_models.Filter         = Filter
    _mock_qdrant_models.FieldCondition = FieldCondition
    _mock_qdrant_models.MatchValue     = MatchValue

    sys.modules["brains.brain_ollama"]  = _mock_brain
    sys.modules["qdrant_client"]        = _mock_qdrant_mod
    sys.modules["qdrant_client.models"] = _mock_qdrant_models

    # Force a fresh infra.memory import so it binds PointStruct from our mock.
    sys.modules.pop("infra.memory", None)
    sys.modules.pop("infra", None)

    from infra.memory import MemoryStore, _COLLECTION_PREFIX
    import infra.memory as mem_mod

    mock_client = MagicMock()
    mock_client.get_collections.return_value = MagicMock(collections=[])
    mem_mod._client_cache = mock_client
    s = MemoryStore()
    s._ready = True
    return s, mock_client


def _mock_embed(text: str):
    return [0.1] * 768


def _make_hit(content: str, score: float = 0.9, layer: str = "personal",
              key: str = "", session_id: str = "", created_at: float = 0.0,
              importance: int = 5):
    h = MagicMock()
    h.id = str(uuid.uuid4())
    h.score = score
    h.payload = {
        "content":    content,
        "layer":      layer,
        "agent_id":   "test",
        "key":        key,
        "importance": importance,
        "created_at": created_at or time.time(),
        "expires_at": None,
        "session_id": session_id,
        "project_id": "",
    }
    return h


# ── write() tests ─────────────────────────────────────────────────────────────

def test_write_calls_embed_and_upsert():
    store, client = _make_store()
    _mock_brain.embed = _mock_embed
    # recall returns nothing (no dedup needed for personal)
    client.search.return_value = []

    entry_id = store.write("personal", "Dark mode preferred", agent_id="human")

    assert entry_id is not None
    client.upsert.assert_called_once()
    call_kwargs = client.upsert.call_args[1]
    assert call_kwargs["collection_name"].endswith("personal")
    point = call_kwargs["points"][0]
    assert point.payload["content"] == "Dark mode preferred"
    assert point.payload["layer"] == "personal"


def test_write_returns_none_when_embed_fails():
    store, client = _make_store()
    _mock_brain.embed = lambda t: None

    result = store.write("working", "some text", agent_id="human", session_id="s1")
    assert result is None
    client.upsert.assert_not_called()


def test_write_knowledge_blocked_if_key_exists_without_force():
    store, client = _make_store()
    _mock_brain.embed = _mock_embed

    # Simulate existing entry found via scroll
    existing = MagicMock()
    existing.id = str(uuid.uuid4())
    existing.payload = {"content": "old", "layer": "knowledge", "agent_id": "a",
                        "key": "mykey", "importance": 5, "created_at": time.time(),
                        "expires_at": None, "session_id": "", "project_id": ""}
    client.scroll.return_value = ([existing], None)

    result = store.write("knowledge", "new content", agent_id="human", key="mykey", force=False)
    client.upsert.assert_not_called()
    assert result == str(existing.id)


def test_write_knowledge_allowed_with_force():
    store, client = _make_store()
    _mock_brain.embed = _mock_embed
    client.scroll.return_value = ([], None)

    result = store.write("knowledge", "new content", agent_id="human", key="mykey", force=True)
    assert result is not None
    client.upsert.assert_called_once()


def test_write_personal_dedup_supersedes_near_duplicate():
    store, client = _make_store()
    _mock_brain.embed = _mock_embed

    # Simulate a near-duplicate at score 0.97
    old_hit = _make_hit("User likes dark mode", score=0.97, layer="personal")
    client.search.return_value = [old_hit]

    store.write("personal", "User prefers dark mode", agent_id="human")
    # Should delete old entry, then upsert new one
    client.delete.assert_called_once()
    client.upsert.assert_called_once()


def test_write_personal_no_dedup_below_threshold():
    store, client = _make_store()
    _mock_brain.embed = _mock_embed

    # Score below 0.95 dedup threshold
    distant_hit = _make_hit("User likes pizza", score=0.40, layer="personal")
    client.search.return_value = [distant_hit]

    store.write("personal", "User prefers dark mode", agent_id="human")
    client.delete.assert_not_called()
    client.upsert.assert_called_once()


def test_write_working_sets_ttl():
    store, client = _make_store()
    _mock_brain.embed = _mock_embed
    client.search.return_value = []
    client.scroll.return_value = ([], None)  # no session entries yet

    before = time.time()
    store.write("working", "temp note", agent_id="human", session_id="sess-1")
    after = time.time()

    point = client.upsert.call_args[1]["points"][0]
    exp = point.payload["expires_at"]
    assert exp is not None
    assert before + 86400 <= exp <= after + 86400


# ── recall() tests ─────────────────────────────────────────────────────────────

def test_recall_returns_hits_above_min_score():
    store, client = _make_store()
    _mock_brain.embed = _mock_embed

    hits = [
        _make_hit("Dark mode preferred",   score=0.85),
        _make_hit("Pizza for lunch",        score=0.20),   # below min_score
    ]
    client.search.return_value = hits

    results = store.recall("UI preferences", layer="personal", min_score=0.30)
    assert len(results) == 1
    assert results[0].content == "Dark mode preferred"
    assert results[0].score == 0.85


def test_recall_filters_expired_entries():
    store, client = _make_store()
    _mock_brain.embed = _mock_embed

    expired = _make_hit("expired note", score=0.9, layer="working")
    expired.payload["expires_at"] = time.time() - 100   # expired 100s ago

    valid = _make_hit("valid note", score=0.8, layer="working")
    valid.payload["expires_at"] = time.time() + 3600

    client.search.return_value = [expired, valid]

    results = store.recall("anything", layer="working")
    assert len(results) == 1
    assert results[0].content == "valid note"


def test_recall_searches_all_layers_when_no_layer_given():
    store, client = _make_store()
    _mock_brain.embed = _mock_embed
    client.search.return_value = []

    store.recall("something")
    assert client.search.call_count == 4   # one call per layer


def test_recall_returns_empty_when_embed_fails():
    store, client = _make_store()
    _mock_brain.embed = lambda t: None

    results = store.recall("anything")
    assert results == []
    client.search.assert_not_called()


# ── forget() tests ─────────────────────────────────────────────────────────────

def test_forget_by_entry_id():
    store, client = _make_store()
    n = store.forget("personal", entry_id="abc-123")
    assert n == 1
    client.delete.assert_called_once()
    assert "abc-123" in str(client.delete.call_args)


def test_forget_by_key():
    store, client = _make_store()
    e = _make_hit("some content", key="mykey")
    client.scroll.return_value = ([MagicMock(id=e.entry_id, payload=e.payload)], None)

    n = store.forget("knowledge", key="mykey")
    assert n == 1
    client.delete.assert_called_once()


def test_forget_returns_zero_when_nothing_found():
    store, client = _make_store()
    client.scroll.return_value = ([], None)

    n = store.forget("project", key="nonexistent")
    assert n == 0


# ── prune() tests ─────────────────────────────────────────────────────────────

def test_prune_working_removes_expired():
    store, client = _make_store()

    expired = MagicMock()
    expired.id = "exp-1"
    expired.payload = {"content": "old", "layer": "working", "agent_id": "a",
                       "key": "", "importance": 3, "created_at": time.time() - 90000,
                       "expires_at": time.time() - 1000, "session_id": "s", "project_id": ""}

    active = MagicMock()
    active.id = "act-1"
    active.payload = {"content": "active", "layer": "working", "agent_id": "a",
                      "key": "", "importance": 5, "created_at": time.time(),
                      "expires_at": time.time() + 3600, "session_id": "s", "project_id": ""}

    client.scroll.return_value = ([expired, active], None)

    removed = store.prune("working")
    assert removed == 1
    client.delete.assert_called_once()


def test_prune_project_removes_stale_low_importance():
    store, client = _make_store()
    stale_time = time.time() - (31 * 86400)   # 31 days ago

    stale = MagicMock()
    stale.id = "stale-1"
    stale.payload = {"content": "old note", "layer": "project", "agent_id": "a",
                     "key": "", "importance": 2,   # below threshold of 3
                     "created_at": stale_time, "expires_at": None,
                     "session_id": "", "project_id": "proj-x"}

    fresh = MagicMock()
    fresh.id = "fresh-1"
    fresh.payload = {"content": "recent", "layer": "project", "agent_id": "a",
                     "key": "", "importance": 2,
                     "created_at": time.time(), "expires_at": None,   # too recent
                     "session_id": "", "project_id": "proj-x"}

    client.scroll.return_value = ([stale, fresh], None)

    removed = store.prune("project")
    assert removed == 1
    client.delete.assert_called_once()


def test_prune_personal_is_noop():
    store, client = _make_store()
    removed = store.prune("personal")
    assert removed == 0
    client.delete.assert_not_called()


def test_prune_knowledge_is_noop():
    store, client = _make_store()
    removed = store.prune("knowledge")
    assert removed == 0
    client.delete.assert_not_called()


# ── summarize_working() tests ─────────────────────────────────────────────────

def test_summarize_working_collapses_oldest_entries():
    store, client = _make_store()
    _mock_brain.embed = _mock_embed
    _mock_brain.ask_local_stream = lambda prompt: iter(["Summary text here"])

    # 15 non-summary entries
    entries = []
    for i in range(15):
        m = MagicMock()
        m.id = f"entry-{i}"
        m.payload = {
            "content": f"entry {i}", "layer": "working", "agent_id": "a",
            "key": "", "importance": 5, "created_at": time.time() - (15 - i) * 60,
            "expires_at": None, "session_id": "sess", "project_id": "",
        }
        entries.append(m)

    # First scroll: session entries for summarize trigger check
    # Second scroll: called inside summarize_working itself
    client.scroll.side_effect = [(entries, None), (entries, None)]
    client.search.return_value = []   # no dedup hits for the summary write

    summary = store.summarize_working("sess", agent_id="human")

    assert summary == "Summary text here"
    assert client.upsert.call_count == 1   # wrote the summary entry
    assert client.delete.call_count == 15  # deleted all source entries


def test_summarize_working_returns_none_below_batch_size():
    store, client = _make_store()

    # Only 5 entries — below the 15-entry batch threshold
    entries = []
    for i in range(5):
        m = MagicMock()
        m.id = f"e-{i}"
        m.payload = {
            "content": f"e{i}", "layer": "working", "agent_id": "a",
            "key": "", "importance": 5, "created_at": time.time(),
            "expires_at": None, "session_id": "sess", "project_id": "",
        }
        entries.append(m)
    client.scroll.return_value = (entries, None)

    result = store.summarize_working("sess")
    assert result is None
    client.upsert.assert_not_called()


# ── context_for_prompt() tests ────────────────────────────────────────────────

def test_context_for_prompt_returns_formatted_block():
    store, client = _make_store()
    _mock_brain.embed = _mock_embed

    client.search.return_value = [_make_hit("Dark mode preferred", score=0.88)]

    ctx = store.context_for_prompt("UI preferences", layer="personal", top_k=3)
    assert "[Memory context]" in ctx
    assert "Dark mode preferred" in ctx
    assert "personal" in ctx
    assert "0.88" in ctx


def test_context_for_prompt_returns_empty_string_when_no_hits():
    store, client = _make_store()
    _mock_brain.embed = _mock_embed
    client.search.return_value = []

    ctx = store.context_for_prompt("anything")
    assert ctx == ""
