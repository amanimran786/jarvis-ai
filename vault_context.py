"""
Deterministic context-pack generation for the Jarvis vault.

Context packs are generated markdown bundles built from a small set of seed
notes and their immediate linked notes. They are meant to give Jarvis a
compact, reusable working set without depending on Obsidian plugin state.
"""

from __future__ import annotations

from pathlib import Path
import re

import vault
import vault_edit


CONTEXT_PACKS_DIR = vault.INDEXES_DIR / "context_packs"
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def _slugify(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return value or "context-pack"


def _extract_links(raw: str) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for match in _WIKILINK_RE.findall(raw or ""):
        ref = match.strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        links.append(ref)
    return links


def _note_snapshot(note_ref: str) -> dict | None:
    path = vault_edit.resolve_note_path(note_ref)
    if not path:
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    cleaned = vault._clean_text(raw)
    rel = str(path.relative_to(vault.VAULT_ROOT))
    return {
        "title": vault._extract_title(path, raw),
        "path": rel,
        "preview": cleaned[:320],
        "links": _extract_links(raw),
    }


def build_context_pack(note_refs: list[str], *, title: str | None = None, max_notes: int = 6) -> dict:
    refs = [ref.strip() for ref in (note_refs or []) if (ref or "").strip()]
    if not refs:
        return {"ok": False, "error": "No seed notes provided for the context pack."}

    queue = list(refs)
    seen_paths: set[str] = set()
    bundle: list[dict] = []

    while queue and len(bundle) < max_notes:
        ref = queue.pop(0)
        snapshot = _note_snapshot(ref)
        if not snapshot or snapshot["path"] in seen_paths:
            continue
        seen_paths.add(snapshot["path"])
        bundle.append(snapshot)
        for link in snapshot["links"]:
            linked = _note_snapshot(link)
            if not linked:
                continue
            if linked["path"] in seen_paths:
                continue
            if linked["path"].startswith("raw/"):
                continue
            queue.append(link)

    if not bundle:
        return {"ok": False, "error": "Could not resolve any notes for the context pack."}

    pack_title = (title or "").strip()
    if not pack_title:
        if len(refs) == 1:
            pack_title = f"{Path(refs[0]).stem} Context Pack"
        else:
            pack_title = f"{Path(refs[0]).stem} Working Context Pack"

    CONTEXT_PACKS_DIR.mkdir(parents=True, exist_ok=True)
    path = CONTEXT_PACKS_DIR / f"{_slugify(pack_title)}.md"

    seed_links = ", ".join(f"[[{ref}]]" for ref in refs)
    related_links = []
    for item in bundle:
        related_links.append(f'"[[{item["title"]}]]"')
    frontmatter = "\n".join(
        [
            "---",
            "type: generated_context_pack",
            "area: vault",
            "owner: generated",
            "write_policy: generated",
            "review_required: false",
            "status: active",
            "source: repo",
            "confidence: high",
            f"created: {_today()}",
            f"updated: {_today()}",
            "version: 1",
            "tags:",
            "  - context-pack",
            "  - generated",
            "  - vault",
            "related:",
            *[f"  - {link}" for link in related_links[:8]],
            "---",
            "",
        ]
    )

    lines = [
        frontmatter,
        f"# {pack_title}",
        "",
        "Purpose: generated working set built from explicit seed notes and their nearest linked notes.",
        "",
        "## Seed Notes",
        "",
        f"- {seed_links}",
        "",
        "## Included Notes",
        "",
    ]
    for item in bundle:
        lines.extend(
            [
                f"### [[{item['title']}]]",
                "",
                f"- path: `{item['path']}`",
                f"- summary: {item['preview']}",
                "",
            ]
        )
    lines.extend(
        [
            "## Usage",
            "",
            "Use this pack when you want a compact working set instead of broad vault retrieval.",
            "",
        ]
    )

    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    vault.refresh_index()
    return {
        "ok": True,
        "title": pack_title,
        "path": str(path.relative_to(vault.VAULT_ROOT)),
        "note_count": len(bundle),
        "seeds": refs,
    }


def _today() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d")
