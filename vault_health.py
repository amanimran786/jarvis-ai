"""
Deterministic health checks for the Jarvis markdown brain.

This keeps the Obsidian layer inspectable: a generated note summarizes graph
health, stale generated context packs, duplicate basenames, and weakly linked
brain notes so Jarvis can maintain the brain without plugin lock-in.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import re

import vault
import vault_edit


_GENERATED_HEALTH_NOTE = "wiki/brain/94 Brain Health.md"
_BRAIN_STATUS_EXCLUDE = {
    "00 Home",
    "02 Brain Dashboard",
    "91 Vault Changelog",
    "92 Agent Inbox",
    "93 Vault Maintenance",
    "94 Brain Health",
}


def _parse_date(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _extract_wikilinks(raw: str) -> list[str]:
    return [item.strip() for item in re.findall(r"\[\[([^\]]+)\]\]", raw or "")]


def _doc_record(path: Path) -> dict | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    rel = str(path.relative_to(vault.VAULT_ROOT))
    meta = vault_edit._frontmatter_metadata(raw)
    title = vault._extract_title(path, raw)
    if title == "---":
        title = path.stem.replace("_", " ").replace("-", " ").strip().title() or path.stem
    return {
        "path": rel,
        "title": title,
        "raw": raw,
        "metadata": meta,
    }


def _alias_values(doc: dict) -> set[str]:
    rel = doc["path"]
    title = doc["title"]
    values = {
        rel.lower(),
        rel[:-3].lower() if rel.lower().endswith(".md") else rel.lower(),
        Path(rel).stem.lower(),
        title.lower(),
    }
    return {value.strip() for value in values if value.strip()}


def _graph_snapshot() -> dict:
    docs = []
    for path in vault._iter_docs():
        record = _doc_record(path)
        if record:
            docs.append(record)

    alias_groups: dict[str, set[str]] = defaultdict(set)
    for doc in docs:
        for alias in _alias_values(doc):
            alias_groups[alias].add(doc["path"])

    alias_map = {alias: next(iter(paths)) for alias, paths in alias_groups.items() if len(paths) == 1}

    outbound: dict[str, set[str]] = defaultdict(set)
    inbound: dict[str, set[str]] = defaultdict(set)

    for doc in docs:
        for link in _extract_wikilinks(doc["raw"]):
            resolved = alias_map.get(link.lower().strip())
            if not resolved or resolved == doc["path"]:
                continue
            outbound[doc["path"]].add(resolved)
            inbound[resolved].add(doc["path"])

    basename_counts = Counter(Path(doc["path"]).name for doc in docs)
    duplicates = []
    for name, count in basename_counts.items():
        if count < 2:
            continue
        matches = sorted(doc["path"] for doc in docs if Path(doc["path"]).name == name)
        duplicates.append({"name": name, "count": count, "paths": matches})

    return {
        "docs": docs,
        "outbound": outbound,
        "inbound": inbound,
        "duplicates": sorted(duplicates, key=lambda item: (-item["count"], item["name"].lower())),
    }


def brain_health_status(stale_context_days: int = 7, weak_degree: int = 1) -> dict:
    threshold = max(int(stale_context_days or 0), 1)
    weak_limit = max(int(weak_degree or 0), 0)
    snapshot = _graph_snapshot()
    docs = snapshot["docs"]
    outbound = snapshot["outbound"]
    inbound = snapshot["inbound"]
    now = datetime.now()

    weak_nodes = []
    orphan_nodes = []
    stale_context_packs = []

    for doc in docs:
        path = doc["path"]
        title = doc["title"]
        meta = doc["metadata"]
        degree = len(outbound.get(path, set())) + len(inbound.get(path, set()))
        is_brain_note = path.startswith("wiki/brain/")
        if is_brain_note and title not in _BRAIN_STATUS_EXCLUDE:
            if degree == 0:
                orphan_nodes.append({"title": title, "path": path, "degree": degree})
            if degree <= weak_limit:
                weak_nodes.append(
                    {
                        "title": title,
                        "path": path,
                        "degree": degree,
                        "inbound": len(inbound.get(path, set())),
                        "outbound": len(outbound.get(path, set())),
                    }
                )

        if not path.startswith("indexes/context_packs/"):
            continue
        updated_at = _parse_date(meta.get("updated") or meta.get("created"))
        if not updated_at:
            continue
        age_days = (now - updated_at).days
        if age_days >= threshold:
            stale_context_packs.append(
                {"title": title, "path": path, "age_days": age_days, "updated": updated_at.strftime("%Y-%m-%d")}
            )

    maintenance = vault_edit.maintenance_status(stale_after_days=3)
    candidates = maintenance.get("candidates", {}) if maintenance.get("ok") else {}
    inbox = maintenance.get("agent_inbox", {}) if maintenance.get("ok") else {}

    return {
        "ok": True,
        "threshold_days": threshold,
        "weak_degree": weak_limit,
        "doc_count": len(docs),
        "brain_note_count": sum(1 for doc in docs if doc["path"].startswith("wiki/brain/")),
        "context_pack_count": sum(1 for doc in docs if doc["path"].startswith("indexes/context_packs/")),
        "duplicate_basenames": snapshot["duplicates"],
        "duplicate_basename_count": len(snapshot["duplicates"]),
        "weak_nodes": sorted(weak_nodes, key=lambda item: (item["degree"], item["title"].lower())),
        "weak_node_count": len(weak_nodes),
        "orphan_nodes": sorted(orphan_nodes, key=lambda item: item["title"].lower()),
        "orphan_node_count": len(orphan_nodes),
        "stale_context_packs": sorted(stale_context_packs, key=lambda item: (-item["age_days"], item["title"].lower())),
        "stale_context_pack_count": len(stale_context_packs),
        "maintenance": {"candidates": candidates, "agent_inbox": inbox},
    }


def refresh_brain_health_note(stale_context_days: int = 7, weak_degree: int = 1) -> dict:
    status = brain_health_status(stale_context_days=stale_context_days, weak_degree=weak_degree)
    if not status.get("ok"):
        return status

    note_path = vault.VAULT_ROOT / _GENERATED_HEALTH_NOTE
    note_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    created = today
    version = 1
    if note_path.exists():
        try:
            existing = note_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        existing_meta = vault_edit._frontmatter_metadata(existing)
        created = existing_meta.get("created", "").strip() or today
        existing_version = existing_meta.get("version", "").strip()
        version = int(existing_version) + 1 if existing_version.isdigit() else 1

    weak_lines = [
        f"- [[{item['title']}]] ({item['degree']} total links; in={item['inbound']}, out={item['outbound']})"
        for item in status["weak_nodes"][:8]
    ] or ["- none"]
    duplicate_lines = [
        f"- `{item['name']}` -> " + ", ".join(f"`{path}`" for path in item["paths"][:4])
        for item in status["duplicate_basenames"][:8]
    ] or ["- none"]
    stale_pack_lines = [
        f"- [[{item['title']}]] ({item['age_days']}d since {item['updated']})"
        for item in status["stale_context_packs"][:8]
    ] or ["- none"]

    maintenance = status["maintenance"]
    candidates = maintenance.get("candidates", {})
    inbox = maintenance.get("agent_inbox", {})

    body = "\n".join(
        [
            "---",
            "type: brain_meta",
            "area: vault",
            "owner: jarvis",
            "write_policy: generated",
            "review_required: false",
            "status: active",
            "source: repo",
            "confidence: high",
            f"created: {created}",
            f"updated: {today}",
            f"version: {version}",
            "tags:",
            "  - vault",
            "  - brain-health",
            "  - generated",
            "related:",
            '  - "[[00 Home]]"',
            '  - "[[02 Brain Dashboard]]"',
            '  - "[[08 Coding Systems Hub]]"',
            '  - "[[09 Jarvis Repo Map]]"',
            '  - "[[12 External Brain Patterns]]"',
            '  - "[[93 Vault Maintenance]]"',
            '  - "[[91 Vault Changelog]]"',
            "---",
            "",
            "# Brain Health",
            "",
            "Purpose: deterministic graph-health snapshot for the Obsidian-compatible Jarvis brain.",
            "",
            "Linked notes: [[00 Home]], [[02 Brain Dashboard]], [[08 Coding Systems Hub]], [[09 Jarvis Repo Map]], [[12 External Brain Patterns]], [[93 Vault Maintenance]], [[91 Vault Changelog]]",
            "",
            "## Overview",
            "",
            f"- docs indexed: {status['doc_count']}",
            f"- curated brain notes: {status['brain_note_count']}",
            f"- context packs: {status['context_pack_count']}",
            f"- weak brain nodes: {status['weak_node_count']}",
            f"- orphan brain nodes: {status['orphan_node_count']}",
            f"- duplicate basenames: {status['duplicate_basename_count']}",
            f"- stale context packs older than {status['threshold_days']} days: {status['stale_context_pack_count']}",
            f"- maintenance snapshot: candidates stale={candidates.get('stale', 0)}, inbox stale={inbox.get('stale', 0)}",
            "",
            "## Weakly Linked Brain Nodes",
            "",
            *weak_lines,
            "",
            "## Duplicate Basenames",
            "",
            *duplicate_lines,
            "",
            "## Stale Context Packs",
            "",
            *stale_pack_lines,
            "",
            "## Recommended Commands",
            "",
            "- Refresh brain health note.",
            "- Refresh vault maintenance dashboard.",
            f"- Review stale vault work older than {status['threshold_days']} days.",
            "- Build context pack for [[20 Projects]] and [[80 Jarvis Roadmap]].",
            "- Build context pack for [[08 Coding Systems Hub]] and [[09 Jarvis Repo Map]].",
            "",
        ]
    )
    try:
        note_path.write_text(body, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not write brain health note: {exc}"}
    vault.refresh_index()
    return {"ok": True, "path": _GENERATED_HEALTH_NOTE, "action": "refreshed"}
