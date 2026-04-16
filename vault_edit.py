"""
Targeted note mutation helpers for the Jarvis vault.

Keep read/search/index behavior in vault.py. This module owns note resolution
and small durable edits so agent-native note changes stay explicit and bounded.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re

import vault


_CANDIDATES_DIR = "wiki/candidates"


def _normalize_note_ref(note_ref: str) -> str:
    text = (note_ref or "").strip()
    text = re.sub(r"^\[\[|\]\]$", "", text)
    text = text[:-3] if text.lower().endswith(".md") else text
    return text.strip()


def _frontmatter_metadata(raw: str) -> dict[str, str]:
    text = raw or ""
    if not text.startswith("---\n"):
        return {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    metadata: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if ":" not in line or line.lstrip().startswith("-"):
            continue
        key, value = line.split(":", 1)
        metadata[key.strip().lower()] = value.strip().strip("\"'")
    return metadata


def _replace_frontmatter_field(raw: str, key: str, value: str) -> str:
    text = raw or ""
    normalized_key = (key or "").strip()
    if not text.startswith("---\n"):
        return text
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    updated = []
    replaced = False
    in_frontmatter = True
    for idx, line in enumerate(lines):
        if idx == 0:
            updated.append(line)
            continue
        if in_frontmatter and line.strip() == "---":
            if not replaced:
                updated.append(f"{normalized_key}: {value}")
            updated.append(line)
            in_frontmatter = False
            continue
        if in_frontmatter and re.match(rf"^{re.escape(normalized_key)}\s*:", line.strip(), re.IGNORECASE):
            updated.append(f"{normalized_key}: {value}")
            replaced = True
            continue
        updated.append(line)
    return "\n".join(updated).rstrip() + "\n"


def _touch_frontmatter(raw: str, *, when: str | None = None) -> str:
    text = raw or ""
    stamp = (when or datetime.now().strftime("%Y-%m-%d")).strip()
    touched = _replace_frontmatter_field(text, "updated", stamp)
    metadata = _frontmatter_metadata(touched)
    version = metadata.get("version", "").strip()
    if version.isdigit():
        touched = _replace_frontmatter_field(touched, "version", str(int(version) + 1))
    return touched


def _parse_iso_date(text: str) -> datetime | None:
    value = (text or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _resolve_note_match(note_ref: str) -> dict:
    normalized = _normalize_note_ref(note_ref)
    if not normalized:
        return {"ok": False, "error": "Missing note reference."}

    direct = vault.VAULT_ROOT / normalized
    if direct.exists() and direct.is_file():
        return {"ok": True, "path": direct}

    direct_md = vault.VAULT_ROOT / f"{normalized}.md"
    if direct_md.exists() and direct_md.is_file():
        return {"ok": True, "path": direct_md}

    candidates: list[tuple[int, Path]] = []
    for path in vault._iter_docs():
        rel = str(path.relative_to(vault.VAULT_ROOT))
        stem = path.stem
        title = vault._extract_title(path, path.read_text(encoding="utf-8", errors="ignore"))
        score = 0
        lower_norm = normalized.lower()
        if rel.lower() == lower_norm.lower() or rel.lower() == f"{lower_norm.lower()}.md":
            score += 100
        if stem.lower() == lower_norm:
            score += 80
        if title.lower() == lower_norm:
            score += 70
        if lower_norm in rel.lower():
            score += 30
        score += vault._path_bias(rel)
        if score > 0:
            candidates.append((score, path))

    if not candidates:
        return {"ok": False, "error": f"Note not found for {note_ref}."}

    candidates.sort(key=lambda item: item[0], reverse=True)
    top_score = candidates[0][0]
    top_paths = [path for score, path in candidates if score == top_score]
    if len(top_paths) > 1:
        rel_paths = [str(path.relative_to(vault.VAULT_ROOT)) for path in top_paths[:4]]
        options = ", ".join(rel_paths)
        return {
            "ok": False,
            "error": f"Ambiguous note reference for {note_ref}. Matches: {options}.",
            "ambiguous": True,
            "candidates": rel_paths,
        }
    return {"ok": True, "path": candidates[0][1]}


def resolve_note_path(note_ref: str) -> Path | None:
    result = _resolve_note_match(note_ref)
    return result.get("path") if result.get("ok") else None


def read_note(note_ref: str, max_chars: int = 1800) -> dict:
    result = _resolve_note_match(note_ref)
    if not result.get("ok"):
        return result
    path = result["path"]
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not read note: {exc}"}
    rel = str(path.relative_to(vault.VAULT_ROOT))
    title = vault._extract_title(path, raw)
    content = raw[:max_chars]
    metadata = _frontmatter_metadata(raw)
    return {
        "ok": True,
        "path": rel,
        "title": title,
        "content": content,
        "truncated": len(raw) > max_chars,
        "metadata": metadata,
    }


def _append_to_raw_under_heading(raw: str, heading: str, content: str) -> str:
    lines = raw.splitlines()
    heading_pattern = re.compile(rf"^#+\s+{re.escape((heading or '').strip())}\s*$", re.IGNORECASE)
    heading_index = None
    heading_level = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if heading_pattern.match(stripped):
            heading_index = idx
            heading_level = len(re.match(r"^(#+)", stripped).group(1))
            break

    block = (content or "").strip()
    if heading_index is None:
        separator = "\n" if raw.endswith("\n") else "\n\n"
        return raw.rstrip() + f"{separator}## {heading.strip()}\n\n{block}\n"

    insert_at = len(lines)
    for idx in range(heading_index + 1, len(lines)):
        stripped = lines[idx].strip()
        heading_match = re.match(r"^(#+)\s+", stripped)
        if heading_match and len(heading_match.group(1)) <= (heading_level or 6):
            insert_at = idx
            break
    new_lines = lines[:insert_at]
    if new_lines and new_lines[-1].strip():
        new_lines.append("")
    new_lines.append(block)
    if insert_at < len(lines) and lines[insert_at - 1].strip():
        new_lines.append("")
    new_lines.extend(lines[insert_at:])
    return "\n".join(new_lines).rstrip() + "\n"


def append_under_heading(note_ref: str, heading: str, content: str) -> dict:
    result = _resolve_note_match(note_ref)
    if not result.get("ok"):
        return result
    path = result["path"]
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not read note: {exc}"}
    metadata = _frontmatter_metadata(raw)
    write_policy = metadata.get("write_policy", "").strip().lower()
    if write_policy == "generated":
        return {
            "ok": False,
            "error": "This note is generated-only. Update the source material or the generator, not the compiled note directly.",
            "write_policy": write_policy,
        }
    if write_policy == "propose_only":
        return {
            "ok": False,
            "error": "This note is propose-only. Route changes into [[92 Agent Inbox]] or a candidate note before updating the canonical note.",
            "write_policy": write_policy,
        }

    block = (content or "").strip()
    if not block:
        return {"ok": False, "error": "No content provided."}

    updated = _append_to_raw_under_heading(raw, heading, block)

    try:
        path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not update note: {exc}"}
    vault.refresh_index()
    return {
        "ok": True,
        "path": str(path.relative_to(vault.VAULT_ROOT)),
        "heading": heading.strip(),
        "action": "appended",
        "write_policy": write_policy or "unspecified",
    }


def stage_candidate_update(note_ref: str, heading: str, content: str, *, reason: str = "propose_only") -> dict:
    result = _resolve_note_match(note_ref)
    if not result.get("ok"):
        return result
    target_path = result["path"]
    canonical_rel = str(target_path.relative_to(vault.VAULT_ROOT))
    candidate_title = f"{target_path.stem} Candidate"
    destination_dir = vault.VAULT_ROOT / _CANDIDATES_DIR
    destination_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = destination_dir / f"{candidate_title}.md"

    if candidate_path.exists():
        existing = candidate_path.read_text(encoding="utf-8")
        existing_meta = _frontmatter_metadata(existing)
        if existing_meta.get("canonical_target") != canonical_rel:
            safe_suffix = re.sub(r"[^A-Za-z0-9]+", " ", canonical_rel).strip().replace(" ", " ")
            candidate_title = f"{target_path.stem} Candidate {safe_suffix}"
            candidate_path = destination_dir / f"{candidate_title}.md"

    if not candidate_path.exists():
        today = datetime.now().strftime("%Y-%m-%d")
        linked_title = target_path.stem
        rendered = "\n".join(
            [
                "---",
                "type: brain_note",
                "area: vault",
                "owner: jarvis",
                "write_policy: append_only",
                "review_required: false",
                "status: draft",
                f"source: {reason}",
                "confidence: medium",
                f"created: {today}",
                f"updated: {today}",
                "version: 1",
                f"canonical_target: {canonical_rel}",
                "tags:",
                "  - brain",
                "  - candidate",
                "related:",
                f'  - "[[{linked_title}]]"',
                "---",
                "",
                f"# {candidate_title}",
                "",
                "Purpose: staged candidate updates for a protected canonical note",
                "",
                f"Linked notes: [[{linked_title}]]",
                "",
                "## Canonical Target",
                "",
                f"- Canonical note: [[{linked_title}]]",
                f"- Canonical path: `{canonical_rel}`",
                "",
                "## Proposed Updates",
                "",
            ]
        )
        candidate_path.write_text(rendered, encoding="utf-8")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    block = "\n".join(
        [
            f"### {timestamp}",
            "",
            f"- Target note: [[{target_path.stem}]]",
            f"- Target heading: {heading.strip()}",
            f"- Reason: {reason}",
            "",
            content.strip(),
        ]
    )
    append_result = append_under_heading(
        str(candidate_path.relative_to(vault.VAULT_ROOT)),
        "Proposed Updates",
        block,
    )
    if not append_result.get("ok"):
        return append_result
    vault.refresh_index()
    return {
        "ok": True,
        "path": str(candidate_path.relative_to(vault.VAULT_ROOT)),
        "title": candidate_title,
        "canonical_target": canonical_rel,
        "heading": "Proposed Updates",
        "action": "staged",
    }


def _extract_heading_section(raw: str, heading: str) -> str:
    lines = raw.splitlines()
    heading_pattern = re.compile(rf"^#+\s+{re.escape((heading or '').strip())}\s*$", re.IGNORECASE)
    heading_index = None
    heading_level = None
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if heading_pattern.match(stripped):
            heading_index = idx
            heading_level = len(re.match(r"^(#+)", stripped).group(1))
            break
    if heading_index is None:
        return ""
    end_index = len(lines)
    for idx in range(heading_index + 1, len(lines)):
        stripped = lines[idx].strip()
        heading_match = re.match(r"^(#+)\s+", stripped)
        if heading_match and len(heading_match.group(1)) <= (heading_level or 6):
            end_index = idx
            break
    return "\n".join(lines[heading_index + 1:end_index]).strip()


def _latest_candidate_payload(section_text: str) -> tuple[str, str]:
    text = (section_text or "").strip()
    if not text:
        return "", ""
    matches = list(re.finditer(r"(?m)^###\s+(.+)$", text))
    if not matches:
        return "", text.strip()
    last = matches[-1]
    timestamp = last.group(1).strip()
    chunk = text[last.start():].strip()
    body = chunk
    split_marker = re.search(r"\n\s*\n", chunk)
    if split_marker:
        body = chunk[split_marker.end():].strip()
    else:
        body = re.sub(r"(?m)^###\s+.+$", "", chunk, count=1).strip()
    return timestamp, body


def promote_candidate_update(candidate_ref: str, canonical_ref: str | None = None, heading: str | None = None) -> dict:
    candidate_match = _resolve_note_match(candidate_ref)
    if not candidate_match.get("ok"):
        return candidate_match
    candidate_path = candidate_match["path"]
    candidate_rel = str(candidate_path.relative_to(vault.VAULT_ROOT))
    if not candidate_rel.replace("\\", "/").lower().startswith(f"{_CANDIDATES_DIR}/"):
        return {"ok": False, "error": "Only candidate notes can be promoted through this path."}

    try:
        candidate_raw = candidate_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not read candidate note: {exc}"}
    candidate_meta = _frontmatter_metadata(candidate_raw)
    canonical_target = (canonical_ref or candidate_meta.get("canonical_target") or "").strip()
    if not canonical_target:
        return {"ok": False, "error": "Candidate note is missing a canonical target."}

    canonical_match = _resolve_note_match(canonical_target)
    if not canonical_match.get("ok"):
        return canonical_match
    canonical_path = canonical_match["path"]
    canonical_rel = str(canonical_path.relative_to(vault.VAULT_ROOT))

    if candidate_meta.get("canonical_target") and canonical_ref:
        expected = candidate_meta.get("canonical_target", "")
        if canonical_rel != expected:
            return {
                "ok": False,
                "error": f"Candidate note points to {expected}, not {canonical_rel}. Use the matching canonical target.",
            }

    proposed_section = _extract_heading_section(candidate_raw, "Proposed Updates")
    timestamp, proposed_body = _latest_candidate_payload(proposed_section)
    if not proposed_body:
        return {"ok": False, "error": "Candidate note does not contain a promotable update under Proposed Updates."}

    target_heading = (heading or "").strip()
    if not target_heading:
        heading_match = re.search(r"(?m)^- Target heading:\s*(.+)$", proposed_section)
        target_heading = heading_match.group(1).strip() if heading_match else "Updates"

    try:
        canonical_raw = canonical_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not read canonical note: {exc}"}

    promoted_block = proposed_body.strip()
    if timestamp:
        promoted_block += f"\n\n_Source: promoted from [[{candidate_path.stem}]] on {timestamp}._"
    updated_canonical = _append_to_raw_under_heading(canonical_raw, target_heading, promoted_block)
    updated_canonical = _touch_frontmatter(updated_canonical, when=datetime.now().strftime("%Y-%m-%d"))
    try:
        canonical_path.write_text(updated_canonical, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not update canonical note: {exc}"}

    promotion_log = "\n".join(
        [
            f"- Promoted to [[{canonical_path.stem}]] under {target_heading}.",
            f"- Canonical path: `{canonical_rel}`",
            f"- Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        ]
    )
    updated_candidate = _append_to_raw_under_heading(candidate_raw, "Promotion Log", promotion_log)
    updated_candidate = _touch_frontmatter(updated_candidate, when=datetime.now().strftime("%Y-%m-%d"))
    try:
        candidate_path.write_text(updated_candidate, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Canonical note updated, but candidate note log failed: {exc}"}

    vault.refresh_index()
    return {
        "ok": True,
        "action": "promoted",
        "candidate_path": candidate_rel,
        "canonical_path": canonical_rel,
        "heading": target_heading,
    }


def review_stale_candidate_notes(max_age_days: int = 3) -> dict:
    threshold = max(int(max_age_days or 0), 1)
    candidate_root = vault.VAULT_ROOT / _CANDIDATES_DIR
    if not candidate_root.exists():
        return {"ok": True, "items": [], "count": 0, "threshold_days": threshold}

    now = datetime.now()
    items: list[dict] = []
    for path in sorted(candidate_root.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        metadata = _frontmatter_metadata(raw)
        if metadata.get("canonical_target", "").strip() == "":
            continue
        if metadata.get("status", "").strip().lower() == "archived":
            continue
        updated_at = _parse_iso_date(metadata.get("updated", "")) or _parse_iso_date(metadata.get("created", ""))
        if not updated_at:
            continue
        age_days = (now.date() - updated_at.date()).days
        if age_days < threshold:
            continue
        recommendation = "promote_or_refresh"
        if "## Promotion Log" in raw:
            recommendation = "archive_or_close"
        elif age_days >= max(threshold * 2, threshold + 3):
            recommendation = "review_or_archive"
        items.append(
            {
                "path": str(path.relative_to(vault.VAULT_ROOT)),
                "title": path.stem,
                "canonical_target": metadata.get("canonical_target", "").strip(),
                "age_days": age_days,
                "status": metadata.get("status", "").strip() or "unknown",
                "recommendation": recommendation,
            }
        )
    return {"ok": True, "items": items, "count": len(items), "threshold_days": threshold}


def review_stale_agent_inbox(max_age_days: int = 3) -> dict:
    threshold = max(int(max_age_days or 0), 1)
    inbox_result = _resolve_note_match("92 Agent Inbox")
    if not inbox_result.get("ok"):
        return {"ok": True, "items": [], "count": 0, "threshold_days": threshold}
    inbox_path = inbox_result["path"]
    try:
        raw = inbox_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not read agent inbox: {exc}"}

    now = datetime.now()
    items: list[dict] = []
    current_heading = ""
    for line in raw.splitlines():
        stripped = line.strip()
        heading_match = re.match(r"^(#+)\s+(.*)$", stripped)
        if heading_match:
            current_heading = heading_match.group(2).strip()
            continue
        task_match = re.match(r"^- \[ \]\s+(.*?)(?:\s+📅\s+(\d{4}-\d{2}-\d{2}))?(?:\s+#.*)?$", stripped)
        if not task_match:
            continue
        task_text = task_match.group(1).strip()
        due_text = task_match.group(2) or ""
        due_at = _parse_iso_date(due_text)
        if not due_at:
            continue
        age_days = (now.date() - due_at.date()).days
        if age_days < threshold:
            continue
        recommendation = "requeue_or_split"
        if (current_heading or "").lower() == "in review":
            recommendation = "promote_or_close"
        elif age_days >= max(threshold * 2, threshold + 3):
            recommendation = "archive_or_close"
        items.append(
            {
                "heading": current_heading or "Queued",
                "text": task_text,
                "due": due_text,
                "age_days": age_days,
                "recommendation": recommendation,
            }
        )
    return {"ok": True, "items": items, "count": len(items), "threshold_days": threshold}


def archive_candidate_note(candidate_ref: str) -> dict:
    candidate_match = _resolve_note_match(candidate_ref)
    if not candidate_match.get("ok"):
        return candidate_match
    candidate_path = candidate_match["path"]
    candidate_rel = str(candidate_path.relative_to(vault.VAULT_ROOT))
    if not candidate_rel.replace("\\", "/").lower().startswith(f"{_CANDIDATES_DIR}/"):
        return {"ok": False, "error": "Only candidate notes can be archived through this path."}
    try:
        raw = candidate_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not read candidate note: {exc}"}
    updated = _replace_frontmatter_field(raw, "status", "archived")
    updated = _touch_frontmatter(updated, when=datetime.now().strftime("%Y-%m-%d"))
    archive_log = f"- Archived on {datetime.now().strftime('%Y-%m-%d %H:%M')}."
    updated = _append_to_raw_under_heading(updated, "Archive Log", archive_log)
    try:
        candidate_path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not archive candidate note: {exc}"}
    vault.refresh_index()
    return {"ok": True, "path": candidate_rel, "action": "archived"}


def _update_agent_inbox_item(task_text: str, *, checked: bool, append_requeue_due: str | None = None) -> dict:
    inbox_result = _resolve_note_match("92 Agent Inbox")
    if not inbox_result.get("ok"):
        return inbox_result
    inbox_path = inbox_result["path"]
    try:
        raw = inbox_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not read agent inbox: {exc}"}

    lines = raw.splitlines()
    target_index = None
    matched_heading = "Queued"
    current_heading = "Queued"
    original_line = ""
    task_pattern = re.compile(r"^- \[ \]\s+(.*?)(?:\s+📅\s+(\d{4}-\d{2}-\d{2}))?(?:\s+(#.*))?$")
    for idx, line in enumerate(lines):
        stripped = line.strip()
        heading_match = re.match(r"^(#+)\s+(.*)$", stripped)
        if heading_match:
            current_heading = heading_match.group(2).strip()
            continue
        match = task_pattern.match(stripped)
        if not match:
            continue
        if match.group(1).strip() == (task_text or "").strip():
            target_index = idx
            matched_heading = current_heading
            original_due = match.group(2) or ""
            original_tags = match.group(3) or "#brain #agent-inbox"
            original_line = line
            break

    if target_index is None:
        return {"ok": False, "error": f"Could not find an open agent inbox item matching '{task_text}'."}

    del lines[target_index]
    updated = "\n".join(lines).rstrip() + "\n"
    completed_line = f"- [{'x' if checked else ' '}] {task_text.strip()} {('📅 ' + original_due) if original_due else ''} {original_tags}".rstrip()
    target_done_heading = "Done"
    updated = _append_to_raw_under_heading(updated, target_done_heading, completed_line)

    if append_requeue_due:
        requeue_line = f"- [ ] {task_text.strip()} 📅 {append_requeue_due} #brain #agent-inbox"
        updated = _append_to_raw_under_heading(updated, "Queued", requeue_line)

    updated = _touch_frontmatter(updated, when=datetime.now().strftime("%Y-%m-%d"))
    try:
        inbox_path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not update agent inbox: {exc}"}
    vault.refresh_index()
    return {
        "ok": True,
        "path": str(inbox_path.relative_to(vault.VAULT_ROOT)),
        "heading": matched_heading,
        "action": "requeued" if append_requeue_due else "closed",
    }


def close_agent_inbox_item(task_text: str) -> dict:
    return _update_agent_inbox_item(task_text, checked=True)


def requeue_agent_inbox_item(task_text: str, due_date: str | None = None) -> dict:
    due = (due_date or datetime.now().strftime("%Y-%m-%d")).strip()
    return _update_agent_inbox_item(task_text, checked=True, append_requeue_due=due)


def maintenance_status(stale_after_days: int = 3) -> dict:
    threshold = max(int(stale_after_days or 0), 1)

    candidate_root = vault.VAULT_ROOT / _CANDIDATES_DIR
    candidate_total = 0
    candidate_archived = 0
    if candidate_root.exists():
        for path in candidate_root.glob("*.md"):
            if path.name.lower() == "readme.md":
                continue
            candidate_total += 1
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                continue
            metadata = _frontmatter_metadata(raw)
            if metadata.get("status", "").strip().lower() == "archived":
                candidate_archived += 1

    stale_candidates = review_stale_candidate_notes(max_age_days=threshold)
    stale_inbox = review_stale_agent_inbox(max_age_days=threshold)

    inbox_counts = {"queued": 0, "in_review": 0, "done": 0}
    inbox_result = _resolve_note_match("92 Agent Inbox")
    if inbox_result.get("ok"):
        inbox_path = inbox_result["path"]
        try:
            raw = inbox_path.read_text(encoding="utf-8")
        except OSError:
            raw = ""
        current_heading = ""
        for line in raw.splitlines():
            stripped = line.strip()
            heading_match = re.match(r"^(#+)\s+(.*)$", stripped)
            if heading_match:
                current_heading = heading_match.group(2).strip().lower()
                continue
            if re.match(r"^- \[[ x]\]\s+", stripped):
                if current_heading == "queued":
                    inbox_counts["queued"] += 1
                elif current_heading == "in review":
                    inbox_counts["in_review"] += 1
                elif current_heading == "done":
                    inbox_counts["done"] += 1

    return {
        "ok": True,
        "stale_after_days": threshold,
        "candidates": {
            "total": candidate_total,
            "archived": candidate_archived,
            "active": max(candidate_total - candidate_archived, 0),
            "stale": (stale_candidates.get("count") or 0) if stale_candidates.get("ok") else 0,
        },
        "agent_inbox": {
            **inbox_counts,
            "stale": (stale_inbox.get("count") or 0) if stale_inbox.get("ok") else 0,
        },
    }


def apply_recommended_actions_for_stale_vault_work(max_age_days: int = 7, max_items: int = 5) -> dict:
    threshold = max(int(max_age_days or 0), 1)
    cap = max(min(int(max_items or 0), 20), 1)

    candidate_review = review_stale_candidate_notes(max_age_days=threshold)
    if not candidate_review.get("ok"):
        return candidate_review
    inbox_review = review_stale_agent_inbox(max_age_days=threshold)
    if not inbox_review.get("ok"):
        return inbox_review

    applied: list[dict] = []
    skipped: list[dict] = []

    for item in candidate_review.get("items") or []:
        if len(applied) >= cap:
            skipped.append(
                {
                    "kind": "candidate",
                    "title": item.get("title") or Path(item.get("path", "")).stem,
                    "reason": "cap_reached",
                }
            )
            continue
        recommendation = (item.get("recommendation") or "").strip()
        title = item.get("title") or Path(item.get("path", "")).stem
        if recommendation not in {"review_or_archive", "archive_or_close"}:
            skipped.append({"kind": "candidate", "title": title, "reason": "requires_manual_review"})
            continue
        archived = archive_candidate_note(item.get("path") or title)
        if archived.get("ok"):
            applied.append({"kind": "candidate", "title": title, "action": "archived"})
        else:
            skipped.append({"kind": "candidate", "title": title, "reason": archived.get("error", "archive_failed")})

    for item in inbox_review.get("items") or []:
        if len(applied) >= cap:
            skipped.append({"kind": "inbox", "title": item.get("text", "").strip(), "reason": "cap_reached"})
            continue
        recommendation = (item.get("recommendation") or "").strip()
        title = item.get("text", "").strip()
        if recommendation not in {"archive_or_close", "promote_or_close"}:
            skipped.append({"kind": "inbox", "title": title, "reason": "requires_manual_review"})
            continue
        closed = close_agent_inbox_item(title)
        if closed.get("ok"):
            applied.append({"kind": "inbox", "title": title, "action": "closed"})
        else:
            skipped.append({"kind": "inbox", "title": title, "reason": closed.get("error", "close_failed")})

    return {
        "ok": True,
        "threshold_days": threshold,
        "max_items": cap,
        "applied": applied,
        "applied_count": len(applied),
        "skipped": skipped,
        "skipped_count": len(skipped),
    }


def refresh_maintenance_dashboard(stale_after_days: int = 3) -> dict:
    threshold = max(int(stale_after_days or 0), 1)
    status = maintenance_status(stale_after_days=threshold)
    if not status.get("ok"):
        return status
    stale_candidates = review_stale_candidate_notes(max_age_days=threshold)
    if not stale_candidates.get("ok"):
        return stale_candidates
    stale_inbox = review_stale_agent_inbox(max_age_days=threshold)
    if not stale_inbox.get("ok"):
        return stale_inbox

    note_path = vault.VAULT_ROOT / "wiki" / "brain" / "93 Vault Maintenance.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    created = today
    version = 1
    if note_path.exists():
        try:
            existing = note_path.read_text(encoding="utf-8")
        except OSError:
            existing = ""
        existing_meta = _frontmatter_metadata(existing)
        created = existing_meta.get("created", "").strip() or today
        existing_version = existing_meta.get("version", "").strip()
        version = int(existing_version) + 1 if existing_version.isdigit() else 1

    candidates = status.get("candidates", {})
    inbox = status.get("agent_inbox", {})
    candidate_lines = [
        f"- [[{Path(item['path']).stem}]] -> `{item['canonical_target']}` ({item['age_days']}d, next: {item.get('recommendation', 'review')})"
        for item in (stale_candidates.get("items") or [])[:8]
    ] or ["- none"]
    inbox_lines = [
        f"- {item['heading']}: {item['text']} (due {item['due']}, {item['age_days']}d, next: {item.get('recommendation', 'review')})"
        for item in (stale_inbox.get("items") or [])[:8]
    ] or ["- none"]

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
            "  - maintenance",
            "  - generated",
            "related:",
            '  - "[[00 Home]]"',
            '  - "[[02 Brain Dashboard]]"',
            '  - "[[03 Brain Schema]]"',
            '  - "[[04 Capture Workflow]]"',
            '  - "[[90 Task Hub]]"',
            '  - "[[92 Agent Inbox]]"',
            '  - "[[91 Vault Changelog]]"',
            "---",
            "",
            "# Vault Maintenance",
            "",
            "Purpose: deterministic maintenance snapshot for the self-sustaining vault lane.",
            "",
            "Linked notes: [[00 Home]], [[02 Brain Dashboard]], [[03 Brain Schema]], [[04 Capture Workflow]], [[90 Task Hub]], [[92 Agent Inbox]], [[91 Vault Changelog]]",
            "",
            "## Overview",
            "",
            f"- stale threshold: {threshold} days",
            f"- candidates: active={candidates.get('active', 0)}, archived={candidates.get('archived', 0)}, stale={candidates.get('stale', 0)}",
            f"- agent inbox: queued={inbox.get('queued', 0)}, in_review={inbox.get('in_review', 0)}, done={inbox.get('done', 0)}, stale={inbox.get('stale', 0)}",
            "",
            "## Stale Candidates",
            "",
            *candidate_lines,
            "",
            "## Stale Inbox",
            "",
            *inbox_lines,
            "",
            "## Recommended Commands",
            "",
            f"- Review stale vault work older than {threshold} days.",
            f"- Apply recommended actions for stale vault work older than {threshold} days.",
            "- Show vault maintenance status.",
            "- Apply recommended action for [[Candidate Note]].",
            "- Apply recommended action: inbox item text",
            "",
        ]
    )
    try:
        note_path.write_text(body, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": f"Could not write maintenance dashboard: {exc}"}
    vault.refresh_index()
    return {"ok": True, "path": str(note_path.relative_to(vault.VAULT_ROOT)), "action": "refreshed"}


def create_note_from_template(title: str, template_name: str = "brain-note-template", destination: str = "wiki/brain") -> dict:
    note_title = (title or "").strip()
    if not note_title:
        return {"ok": False, "error": "Missing note title."}
    template_path = vault.TEMPLATES_DIR / f"{template_name}.md"
    if not template_path.exists():
        return {"ok": False, "error": f"Template not found: {template_name}."}

    destination_dir = vault.VAULT_ROOT / destination
    destination_dir.mkdir(parents=True, exist_ok=True)
    file_name = note_title.replace("/", "-").strip() + ".md"
    note_path = destination_dir / file_name
    if note_path.exists():
        return {"ok": False, "error": f"Note already exists: {note_path.relative_to(vault.VAULT_ROOT)}."}

    template = template_path.read_text(encoding="utf-8")
    rendered = (
        template
        .replace("<Title>", note_title)
        .replace("<YYYY-MM-DD>", datetime.now().strftime("%Y-%m-%d"))
        .replace("<area>", "vault")
        .replace("<target>", "general")
        .replace("<Decision Name>", note_title)
        .replace("<Story Name>", note_title)
        .replace("<Project Capture>", note_title)
        .replace("<Investigation Name>", note_title)
    )
    note_path.write_text(rendered.rstrip() + "\n", encoding="utf-8")
    vault.refresh_index()
    return {
        "ok": True,
        "path": str(note_path.relative_to(vault.VAULT_ROOT)),
        "title": note_title,
        "template": template_name,
    }
