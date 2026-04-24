"""
jarvis_extractor.py — Conversation fact extractor for Iron Man Jarvis.

Runs after every completed conversation turn (fire-and-forget, background thread).
Uses the fastest available local model to detect extractable facts in the exchange,
then writes them to the appropriate brain notes and mem0.

What gets extracted:
  - New projects or tasks mentioned ("I'm starting X", "we need to do Y by Z")
  - Decisions made ("I decided to...", "we're going with...")
  - Preference updates ("I prefer...", "from now on...", "I want you to...")
  - Important entities (people, companies, deadlines)

What does NOT get extracted:
  - Generic conversation filler
  - Questions without answers
  - Anything already in brain notes (dedup by checking for the key phrase)

Architecture:
  extract_async(user_input, assistant_reply) — fire-and-forget thread
  extract(user_input, assistant_reply)       — synchronous, returns list[dict]
  _write_extractions(facts)                  — routes each fact to vault/mem0

Each fact dict:
  { "type": "task"|"decision"|"preference"|"entity",
    "content": str,
    "confidence": "high"|"medium" }
"""

from __future__ import annotations

import threading
from typing import Any

# ── Extraction prompt ──────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = """\
You are a memory extraction assistant for Jarvis. Given a conversation turn, \
identify any facts that should be saved permanently. Output ONLY a JSON array. \
Each item must have: "type" (task/decision/preference/entity), "content" (the fact \
in one clear sentence), "confidence" (high/medium). \
Skip chitchat, questions, and anything vague. If nothing is worth saving, output [].
Example output:
[
  {"type": "preference", "content": "Aman prefers Qwen3 for fast local tasks", "confidence": "high"},
  {"type": "task", "content": "Research Devstral performance benchmarks", "confidence": "medium"}
]"""

_MAX_TURN_CHARS = 1200   # truncate long turns to keep the model call fast
_CONFIDENCE_THRESHOLD = "medium"  # skip "low" if we add it later

# ── Writer routing ────────────────────────────────────────────────────────────

def _write_extractions(facts: list[dict]) -> None:
    """Route extracted facts to vault_capture and mem0."""
    if not facts:
        return

    try:
        import vault_capture
        import mem0_layer as _m0
    except Exception:
        return

    for fact in facts:
        ftype   = fact.get("type", "entity")
        content = (fact.get("content") or "").strip()
        if not content:
            continue

        # Always write to mem0 — it handles dedup via vector similarity
        try:
            _m0.add_async(content)
        except Exception:
            pass

        # Route to vault based on fact type
        try:
            if ftype == "task":
                vault_capture.add_task(content)
            elif ftype == "decision":
                vault_capture.log_decision(
                    title=content[:80],
                    context="Extracted from conversation",
                    decision=content,
                    rationale="",
                    outcome="pending",
                )
            elif ftype == "preference":
                # Append to 30 Preferences under a catch-all heading
                vault_capture.append_to_note(
                    note_title="30 Preferences",
                    heading="## Captured Preferences",
                    content=f"- {content}",
                )
        except Exception:
            pass


# ── Extractor ─────────────────────────────────────────────────────────────────

def _run_extraction(user_input: str, assistant_reply: str) -> list[dict]:
    """Run the LLM extraction pass. Returns list of fact dicts."""
    turn = (
        f"User: {user_input.strip()[:600]}\n"
        f"Jarvis: {assistant_reply.strip()[:600]}"
    )
    try:
        import model_router as mr
        import json

        chunks: list[str] = []
        stream, _ = mr.smart_stream(
            turn,
            tool="extraction",
            extra_system=_EXTRACT_SYSTEM,
        )
        for chunk in stream:
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > 800:
                break

        raw = "".join(chunks).strip()

        # Pull JSON array out of response (model may wrap in markdown)
        start = raw.find("[")
        end   = raw.rfind("]") + 1
        if start == -1 or end == 0:
            return []

        facts = json.loads(raw[start:end])
        if not isinstance(facts, list):
            return []

        # Filter low-confidence and malformed items
        valid: list[dict] = []
        for item in facts:
            if not isinstance(item, dict):
                continue
            if not item.get("content", "").strip():
                continue
            if item.get("confidence") == "low":
                continue
            valid.append(item)

        return valid
    except Exception:
        return []


# ── Public API ────────────────────────────────────────────────────────────────

def extract(user_input: str, assistant_reply: str) -> list[dict]:
    """Synchronous extraction. Returns list of fact dicts written to vault/mem0."""
    if not user_input or not assistant_reply:
        return []
    # Skip very short / trivial exchanges
    combined = user_input.strip() + " " + assistant_reply.strip()
    if len(combined) < 60:
        return []
    facts = _run_extraction(user_input, assistant_reply)
    if facts:
        _write_extractions(facts)
    return facts


def extract_async(user_input: str, assistant_reply: str) -> None:
    """Fire-and-forget background extraction. Does not block the response path."""
    if not user_input or not assistant_reply:
        return
    combined = user_input.strip() + " " + assistant_reply.strip()
    if len(combined) < 60:
        return
    t = threading.Thread(
        target=extract,
        args=(user_input, assistant_reply),
        daemon=True,
        name="jarvis-extractor",
    )
    t.start()
