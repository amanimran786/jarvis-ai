"""
Generic tamper-evident append-only hash chain for JSONL audit ledgers.

Extracted from infra/pipeline_audit.py so multiple audit streams (pipeline
truthfulness, security_audit S5, …) share one verified tamper-evidence
mechanism instead of each rolling their own.

Model: each record is a JSON object. When appended it gains two fields:
  prev_hash : the previous record's this_hash (or GENESIS for the first)
  this_hash : sha256(prev_hash + canonical(record_without_this_hash))
Editing any past record changes its canonical form, so its this_hash (and every
hash after it) no longer recomputes — verify_chain() reports the break.

Format is intentionally identical to what pipeline_audit wrote before the
extraction, so existing ledgers verify unchanged.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

GENESIS = "GENESIS"
HASH_FIELD = "this_hash"
PREV_FIELD = "prev_hash"


def canonical(obj: Any) -> str:
    """Deterministic JSON for hashing: sorted keys, no whitespace, ascii."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_hash(prev_hash: str, record_without_hash: dict) -> str:
    return sha256_hex(prev_hash + canonical(record_without_hash))


def read_chain(path: Path) -> list[dict]:
    """Load a JSONL ledger. Unparseable lines become {'_corrupt': <raw>} so the
    caller can detect truncation/corruption rather than silently skipping."""
    if not path.exists():
        return []
    out: list[dict] = []
    for ln in path.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except Exception:
            out.append({"_corrupt": ln})
    return out


def append_record(path: Path, record: dict, entries: list[dict] | None = None) -> dict:
    """Append `record` chained to the last entry's hash and return the stored
    record (with prev_hash/this_hash set). If `entries` is None it is read from
    disk first."""
    if entries is None:
        entries = read_chain(path)
    prev = entries[-1].get(HASH_FIELD, GENESIS) if entries else GENESIS
    record = dict(record)
    record[PREV_FIELD] = prev
    record[HASH_FIELD] = compute_hash(prev, record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(canonical(record) + "\n")
    return record


def verify_chain(entries: list[dict]) -> tuple[bool, str | None, int]:
    """Recompute the chain. Returns (ok, reason, index). On the first broken
    link, ok=False with a human reason and the offending index; everything after
    that point is unverifiable. Empty/clean chains return (True, None, -1)."""
    prev = GENESIS
    for idx, e in enumerate(entries):
        if "_corrupt" in e:
            return False, "unparseable ledger line", idx
        if e.get(PREV_FIELD) != prev:
            return False, "prev_hash does not match preceding record", idx
        stored = e.get(HASH_FIELD)
        body = {k: v for k, v in e.items() if k != HASH_FIELD}
        if stored != compute_hash(prev, body):
            return False, "hash chain broken (record edited after the fact)", idx
        prev = stored
    return True, None, -1
