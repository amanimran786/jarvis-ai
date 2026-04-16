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
