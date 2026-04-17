"""
Deterministic specialist handoff notes for note-scoped work.

These notes keep useful specialist output out of chat-only history and inside
the markdown brain, but only when a task is explicitly scoped to one or more
wikilinked notes.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

import vault
import vault_edit


_HANDOFF_DIR = "indexes/handoffs"
_SUPPORTED_ROLES = {
    "coder",
    "code_reviewer",
    "debugger",
    "researcher",
    "vault_curator",
    "security_analyst",
    "operator",
}


def extract_note_refs(task: str) -> list[str]:
    refs = [item.strip() for item in re.findall(r"\[\[([^\]]+)\]\]", task or "")]
    deduped: list[str] = []
    for ref in refs:
        if ref and ref not in deduped:
            deduped.append(ref)
    return deduped


def should_record_handoff(role: str, task: str) -> bool:
    return role in _SUPPORTED_ROLES and bool(extract_note_refs(task))


def _safe_stem(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", " ", (value or "").strip()).strip()
    return text.title() or "Untitled"


def _trim(text: str, limit: int = 900) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[: limit - 4].rstrip() + " ..."


def record_role_handoff(role: str, task: str, output: str) -> dict:
    refs = extract_note_refs(task)
    if not refs:
        return {"ok": False, "error": "No explicit note references found for handoff."}

    primary = refs[0]
    resolved_primary = vault_edit.resolve_note_path(primary)
    primary_note_path = (
        str(resolved_primary.relative_to(vault.VAULT_ROOT))
        if resolved_primary is not None
        else ""
    )
    title = f"{_safe_stem(Path(primary).stem)} {role.replace('_', ' ').title()} Handoff"
    destination_dir = vault.VAULT_ROOT / _HANDOFF_DIR
    destination_dir.mkdir(parents=True, exist_ok=True)
    note_path = destination_dir / f"{title}.md"

    today = datetime.now().strftime("%Y-%m-%d")
    created = today
    version = 1
    if note_path.exists():
        try:
            existing = note_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        meta = vault_edit._frontmatter_metadata(existing)
        created = meta.get("created", "").strip() or today
        existing_version = meta.get("version", "").strip()
        version = int(existing_version) + 1 if existing_version.isdigit() else 1

    related_lines = ['  - "[[{}]]"'.format(ref) for ref in refs]
    linked_line = ", ".join(f"[[{ref}]]" for ref in refs)
    body = "\n".join(
        [
            "---",
            "type: generated_handoff",
            "area: vault",
            "owner: generated",
            "write_policy: generated",
            "review_required: false",
            "status: active",
            "source: repo",
            "confidence: medium",
            f"created: {created}",
            f"updated: {today}",
            f"version: {version}",
            f"role: {role}",
            f"primary_note_path: {primary_note_path}",
            "tags:",
            "  - handoff",
            "  - generated",
            f"  - {role}",
            "related:",
            *related_lines,
            "---",
            "",
            f"# {title}",
            "",
            "Purpose: compact durable handoff for note-scoped specialist work.",
            "",
            f"Linked notes: {linked_line}",
            "",
            "## Task",
            "",
            _trim(task, limit=500),
            "",
            "## Output",
            "",
            _trim(output, limit=1200) or "No output captured.",
            "",
            "## Next Step",
            "",
            f"- Review this handoff against {linked_line} before promoting any canonical change.",
            "",
        ]
    )
    try:
        note_path.write_text(body, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not write handoff note: {exc}"}
    vault.refresh_index()
    return {
        "ok": True,
        "path": str(note_path.relative_to(vault.VAULT_ROOT)),
        "title": title,
        "role": role,
        "notes": refs,
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


def _handoff_docs_for(note_ref: str) -> list[dict]:
    target_path = vault_edit.resolve_note_path(note_ref)
    if target_path is None:
        return []
    target_rel = str(target_path.relative_to(vault.VAULT_ROOT))
    target_stem = target_path.stem.lower()
    handoff_root = vault.VAULT_ROOT / _HANDOFF_DIR
    if not handoff_root.exists():
        return []
    matches = []
    for path in sorted(handoff_root.glob("*.md")):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        meta = vault_edit._frontmatter_metadata(raw)
        if meta.get("type", "").strip() != "generated_handoff":
            continue
        primary_note_path = meta.get("primary_note_path", "").strip()
        if primary_note_path:
            if primary_note_path != target_rel:
                continue
        else:
            links = extract_note_refs(raw)
            if not any(Path(link).stem.lower() == target_stem for link in links):
                continue
        title = vault._extract_title(path, raw)
        if title == "---":
            title = path.stem.replace("_", " ").replace("-", " ").strip().title() or path.stem
        matches.append(
            {
                "path": str(path.relative_to(vault.VAULT_ROOT)),
                "title": title,
                "updated": meta.get("updated", "").strip(),
                "role": meta.get("role", "").strip() or "specialist",
                "task": _section_text(raw, "Task"),
                "output": _section_text(raw, "Output"),
            }
        )
    matches.sort(key=lambda item: (_parse_date(item["updated"]) or datetime.min), reverse=True)
    return matches


def _section_text(raw: str, heading: str) -> str:
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)",
        re.MULTILINE,
    )
    match = pattern.search(raw or "")
    if not match:
        return ""
    return match.group(1).strip()


def compact_handoffs(note_ref: str, *, max_handoffs: int = 5) -> dict:
    normalized = (note_ref or "").strip()
    if not normalized:
        return {"ok": False, "error": "Missing note reference for handoff compaction."}

    target_path = vault_edit.resolve_note_path(normalized)
    if target_path is None:
        read_result = vault_edit.read_note(normalized, max_chars=120)
        return {
            "ok": False,
            "error": read_result.get("error", f"Could not resolve [[{Path(normalized).stem}]]."),
            "ambiguous": bool(read_result.get("ambiguous")),
            "candidates": read_result.get("candidates", []),
        }

    docs = _handoff_docs_for(normalized)
    if not docs:
        return {"ok": False, "error": f"No handoff notes found for [[{target_path.stem}]]."}

    limit = max(1, int(max_handoffs or 0))
    selected = docs[:limit]
    primary = target_path.stem
    title = f"{_safe_stem(primary)} Handoff Context Pack"
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "handoff-context-pack"
    destination = vault.INDEXES_DIR / "context_packs"
    destination.mkdir(parents=True, exist_ok=True)
    note_path = destination / f"{slug}.md"

    today = datetime.now().strftime("%Y-%m-%d")
    created = today
    version = 1
    if note_path.exists():
        try:
            existing = note_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        meta = vault_edit._frontmatter_metadata(existing)
        created = meta.get("created", "").strip() or today
        existing_version = meta.get("version", "").strip()
        version = int(existing_version) + 1 if existing_version.isdigit() else 1

    lines = [
        "---",
        "type: generated_context_pack",
        "area: vault",
        "owner: generated",
        "write_policy: generated",
        "review_required: false",
        "status: active",
        "source: repo",
        "confidence: medium",
        f"created: {created}",
        f"updated: {today}",
        f"version: {version}",
        "tags:",
        "  - context-pack",
        "  - generated",
        "  - handoff",
        "related:",
        f'  - "[[{primary}]]"',
        *[f'  - "[[{item["title"]}]]"' for item in selected[:6]],
        "---",
        "",
        f"# {title}",
        "",
        "Purpose: compact working set synthesized from note-scoped specialist handoff notes.",
        "",
        f"Linked notes: [[{primary}]]",
        "",
        "## Source Handoffs",
        "",
    ]
    for item in selected:
        lines.extend(
            [
                f"- [[{item['title']}]] ({item['role']}, updated {item['updated'] or 'unknown'})",
            ]
        )
    lines.extend(
        [
            "",
            "## Compacted Handoffs",
            "",
        ]
    )
    for item in selected:
        lines.extend(
            [
                f"### [[{item['title']}]]",
                "",
                f"- role: {item['role']}",
                f"- updated: {item['updated'] or 'unknown'}",
                f"- task: {_trim(item['task'], limit=320) or 'No task captured.'}",
                f"- output: {_trim(item['output'], limit=520) or 'No output captured.'}",
                "",
            ]
        )
    lines.extend(
        [
            "## Usage",
            "",
            f"Use this pack when you want the recent specialist history for [[{primary}]] without reading every handoff note separately.",
            "",
        ]
    )
    try:
        note_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not write compacted handoff pack: {exc}"}
    vault.refresh_index()
    return {
        "ok": True,
        "path": str(note_path.relative_to(vault.VAULT_ROOT)),
        "title": title,
        "note_ref": normalized,
        "handoff_count": len(selected),
    }
