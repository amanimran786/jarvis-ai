"""
Native tool shortcuts for specialist roles.

These hooks handle explicit, low-ambiguity requests without paying for a model
call. Keep them narrow and deterministic.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import re

import browser
import hardware
import notes
import terminal
import tools
import vault_capture
import vault_edit


_ADMIN_SHELL_PATTERNS = (
    "sudo ",
    "sudo\n",
    "administrator privileges",
    "admin privileges",
    "as admin",
    "run as root",
    "root ",
)


def run_native_role_hook(role: str, task: str) -> dict | None:
    if role == "vault_curator":
        return _run_vault_curator_hook(task)
    if role == "operator":
        return _run_operator_hook(task)
    return None


def _extract_wikilink(task: str) -> str:
    match = re.search(r"\[\[([^\]]+)\]\]", task)
    return match.group(1).strip() if match else ""


def _extract_wikilinks(task: str) -> list[str]:
    return [match.strip() for match in re.findall(r"\[\[([^\]]+)\]\]", task)]


def _run_vault_curator_hook(task: str) -> dict | None:
    lower = task.lower()
    note_ref = _extract_wikilink(task)

    if "apply recommended actions" in lower and ("stale vault work" in lower or "stale vault items" in lower):
        batch_result = _apply_recommended_actions_batch(task)
        if batch_result is not None:
            return {"model": "native/vault_curator", "output": batch_result}

    if "apply recommended action" in lower:
        recommended_result = _apply_recommended_action(task)
        if recommended_result is not None:
            return {"model": "native/vault_curator", "output": recommended_result}

    if "maintenance status" in lower or "vault status" in lower:
        status_result = _maintenance_status(task)
        if status_result is not None:
            return {"model": "native/vault_curator", "output": status_result}

    if "maintenance dashboard" in lower and any(verb in lower for verb in ("refresh", "update", "build", "create")):
        dashboard_result = _refresh_maintenance_dashboard(task)
        if dashboard_result is not None:
            return {"model": "native/vault_curator", "output": dashboard_result}

    if "promote" in lower and "candidate" in lower:
        promotion_result = _promote_candidate_note(task)
        if promotion_result is not None:
            return {"model": "native/vault_curator", "output": promotion_result}

    if "archive" in lower and "candidate" in lower:
        archive_result = _archive_candidate_note(task)
        if archive_result is not None:
            return {"model": "native/vault_curator", "output": archive_result}

    if "requeue inbox item" in lower or "close inbox item" in lower:
        inbox_action_result = _apply_inbox_item_action(task)
        if inbox_action_result is not None:
            return {"model": "native/vault_curator", "output": inbox_action_result}

    if (
        "stale candidate" in lower
        or "stale inbox" in lower
        or "stale vault work" in lower
        or "stale vault items" in lower
    ):
        maintenance_result = _review_stale_vault_work(task)
        if maintenance_result is not None:
            return {"model": "native/vault_curator", "output": maintenance_result}

    if "canonical" in lower and "mark" in lower:
        canonical_result = _mark_canonical_note(task)
        if canonical_result is not None:
            return {"model": "native/vault_curator", "output": canonical_result}

    if "agent inbox" in lower and any(verb in lower for verb in ("add", "queue", "log", "capture")):
        inbox_result = _add_agent_inbox_item(task)
        if inbox_result is not None:
            return {"model": "native/vault_curator", "output": inbox_result}

    if "create disambiguation note" in lower:
        note_result = _create_disambiguation_note(task)
        if note_result is not None:
            return {"model": "native/vault_curator", "output": note_result}

    if "link these candidates" in lower:
        link_result = _link_ambiguous_candidates(task, lower)
        if link_result is not None:
            return {"model": "native/vault_curator", "output": link_result}

    if note_ref and re.search(r"\b(read|show|open)\b", lower):
        result = vault_edit.read_note(note_ref)
        preferred = _prefer_brain_candidate(result, lower)
        if preferred:
            result = vault_edit.read_note(preferred)
        if not result.get("ok"):
            return {"model": "native/vault_curator", "output": _vault_error_text(result, "Could not read that note.")}
        preview = result.get("content", "").strip().replace("\n", " ")
        if result.get("truncated"):
            preview += " ..."
        return {
            "model": "native/vault_curator",
            "output": f"Read {result['title']} from {result['path']}. {preview}".strip(),
        }

    append_match = re.search(
        r"\b(?:append|add|update)\b\s+(?:to\s+)?\[\[([^\]]+)\]\](?:\s+under\s+([^:]+))?\s*:\s*(.+)$",
        task,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if append_match:
        target = append_match.group(1).strip()
        heading = (append_match.group(2) or "Updates").strip()
        content = append_match.group(3).strip()
        note_snapshot = vault_edit.read_note(target, max_chars=200)
        note_meta = note_snapshot.get("metadata", {}) if note_snapshot.get("ok") else {}
        review_required = str(note_meta.get("review_required", "")).lower() == "true"
        write_policy = str(note_meta.get("write_policy", "")).lower()
        if review_required and write_policy not in {"append_only", "generated", "propose_only"}:
            staged = vault_edit.stage_candidate_update(target, heading, content, reason="review_required")
            if staged.get("ok"):
                return {
                    "model": "native/vault_curator",
                    "output": (
                        f"Staged a candidate update for [[{target}]] in {staged['path']} "
                        f"under {staged['heading']} because that note requires review."
                    ),
                }
        result = vault_edit.append_under_heading(target, heading, content)
        if not result.get("ok") and result.get("write_policy") == "propose_only":
            staged = vault_edit.stage_candidate_update(target, heading, content, reason="propose_only")
            if staged.get("ok"):
                return {
                    "model": "native/vault_curator",
                    "output": (
                        f"Staged a candidate update for [[{target}]] in {staged['path']} "
                        f"under {staged['heading']}."
                    ),
                }
        return {
            "model": "native/vault_curator",
            "output": (
                f"Updated {result['path']} under {result['heading']}."
                if result.get("ok")
                else _vault_error_text(result, "Could not update that note.")
            ),
        }

    create_match = re.search(
        r"\b(?:create|make)\b\s+(?:a\s+)?(?:brain\s+)?note(?:\s+called|\s+named)?\s+(.+?)(?:\s+from\s+template\s+([a-z0-9_-]+))?$",
        task.strip(),
        flags=re.IGNORECASE,
    )
    if create_match:
        title = create_match.group(1).strip().strip(".")
        template_name = (create_match.group(2) or "brain-note-template").strip()
        result = vault_edit.create_note_from_template(title, template_name=template_name)
        return {
            "model": "native/vault_curator",
            "output": (
                f"Created {result['title']} at {result['path']} using {result['template']}."
                if result.get("ok")
                else _vault_error_text(result, "Could not create that note.")
            ),
        }

    return None


def _run_operator_hook(task: str) -> dict | None:
    lower = task.lower().strip()

    explicit_admin_match = re.search(
        r"\b(?:run|execute)\b\s+admin\s+(?:command|shell)\s*:?\s+(.+)$",
        task,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if explicit_admin_match:
        return {
            "model": "native/operator",
            "output": "Operator cannot run administrator commands. Use the dedicated admin command path with the exact command instead.",
        }

    note_match = re.search(r"\b(?:take|save|write)\b\s+(?:a\s+)?note\s*:?\s+(.+)$", task, flags=re.IGNORECASE | re.DOTALL)
    if note_match:
        return {"model": "native/operator", "output": notes.add_note(note_match.group(1).strip())}

    search_notes_match = re.search(r"\bsearch notes for\s+(.+)$", task, flags=re.IGNORECASE)
    if search_notes_match:
        return {"model": "native/operator", "output": notes.search_notes(search_notes_match.group(1).strip())}

    if re.search(r"\b(show|get|read)\s+(my\s+)?notes\b", lower):
        return {"model": "native/operator", "output": notes.get_notes()}

    command_match = re.search(r"\b(?:run|execute)\b\s+(?:command|shell)\s*:?\s+(.+)$", task, flags=re.IGNORECASE | re.DOTALL)
    if command_match:
        command = command_match.group(1).strip()
        if _looks_like_admin_shell(command):
            return {
                "model": "native/operator",
                "output": "Operator cannot run administrator commands. Use the dedicated admin command path with the exact command instead.",
            }
        return {"model": "native/operator", "output": terminal.run_command(command)}

    browser_match = re.search(r"\b(?:browse to|open website|open site|go to)\b\s+(.+)$", task, flags=re.IGNORECASE)
    if browser_match:
        return {"model": "native/operator", "output": browser.open_url(browser_match.group(1).strip())}

    settings_match = re.search(r"\bopen system settings(?: for)?\s+(.+)$", task, flags=re.IGNORECASE)
    if settings_match:
        return {"model": "native/operator", "output": hardware.open_system_settings(settings_match.group(1).strip())}

    app_match = re.search(r"\b(?:open|launch|start)\b\s+([A-Za-z][A-Za-z0-9 ._-]{1,80})$", task.strip(), flags=re.IGNORECASE)
    if app_match and not re.search(r"\b(website|site|https?://|www\.)", lower):
        return {"model": "native/operator", "output": tools.open_app(app_match.group(1).strip())}

    return None


def _looks_like_admin_shell(command: str) -> bool:
    lower = (command or "").lower()
    return any(pattern in lower for pattern in _ADMIN_SHELL_PATTERNS)


def _vault_error_text(result: dict, default: str) -> str:
    base = result.get("error", default)
    candidates = result.get("candidates") or []
    if candidates:
        options = ", ".join(f"[[{Path(path).stem}]]" for path in candidates[:4])
        return f"{base} Try one of: {options}."
    return base


def _prefer_brain_candidate(result: dict, lower_task: str) -> str | None:
    if not result.get("ambiguous"):
        return None
    if not any(phrase in lower_task for phrase in ("pick the brain note", "prefer the brain note", "use the brain note")):
        return None
    candidates = result.get("candidates") or []
    brain_candidates = [path for path in candidates if str(path).replace("\\", "/").lower().startswith("wiki/brain/")]
    if len(brain_candidates) == 1:
        return brain_candidates[0]
    return None


def _link_ambiguous_candidates(task: str, lower_task: str) -> str | None:
    links = _extract_wikilinks(task)
    if len(links) < 2:
        return "To link ambiguous candidates, tell me the ambiguous note and the target note, for example: Read [[Roadmap]] and link these candidates in [[90 Task Hub]]."

    source_ref = links[0]
    target_ref = links[1]
    heading_match = re.search(r"\bunder\s+([^:.]+)", task, flags=re.IGNORECASE)
    heading = (heading_match.group(1).strip() if heading_match else "Disambiguation").strip()

    result = vault_edit.read_note(source_ref)
    if not result.get("ambiguous"):
        return _vault_error_text(result, "There were no ambiguous candidates to link.")

    candidates = result.get("candidates") or []
    if not candidates:
        return _vault_error_text(result, "There were no ambiguous candidates to link.")

    block_lines = [f"Disambiguation for [[{source_ref}]]:"]
    block_lines.extend(f"- [[{Path(path).stem}]]" for path in candidates[:6])
    append_result = vault_edit.append_under_heading(target_ref, heading, "\n".join(block_lines))
    if not append_result.get("ok"):
        return _vault_error_text(append_result, "Could not link those candidates.")
    return f"Linked candidate notes for [[{source_ref}]] into {append_result['path']} under {append_result['heading']}."


def _create_disambiguation_note(task: str) -> str | None:
    links = _extract_wikilinks(task)
    if not links:
        return "To create a disambiguation note, tell me which ambiguous note to resolve, for example: Create disambiguation note for [[Roadmap]]."

    source_ref = links[0]
    result = vault_edit.read_note(source_ref)
    if not result.get("ambiguous"):
        return _vault_error_text(result, "There were no ambiguous candidates to turn into a disambiguation note.")

    candidates = result.get("candidates") or []
    if not candidates:
        return _vault_error_text(result, "There were no ambiguous candidates to turn into a disambiguation note.")

    title = f"{source_ref} Disambiguation"
    created = vault_edit.create_note_from_template(title, template_name="brain-note-template")
    if not created.get("ok"):
        return _vault_error_text(created, "Could not create the disambiguation note.")

    candidate_lines = [f"- [[{Path(path).stem}]]" for path in candidates[:8]]
    holds_block = "\n".join(
        [
            f"Disambiguation for [[{source_ref}]].",
            "Candidate notes:",
            *candidate_lines,
        ]
    )
    evidence_block = "\n".join(
        [
            f"Ambiguous source reference: [[{source_ref}]].",
            f"Resolved from candidates: {', '.join(candidates[:8])}.",
        ]
    )
    question_block = f"- [ ] Decide which note should become the canonical target for [[{source_ref}]] #brain"

    for heading, block in (
        ("What This Note Holds", holds_block),
        ("Evidence", evidence_block),
        ("Open Questions", question_block),
    ):
        append_result = vault_edit.append_under_heading(created["path"], heading, block)
        if not append_result.get("ok"):
            return _vault_error_text(append_result, "Created the note but could not finish populating the disambiguation sections.")

    return f"Created disambiguation note [[{title}]] at {created['path']}."


def _mark_canonical_note(task: str) -> str | None:
    links = _extract_wikilinks(task)
    if len(links) < 2:
        return (
            "To mark a canonical note, tell me the chosen note and the disambiguation note, "
            "for example: Mark [[80 Jarvis Roadmap]] as canonical in [[Roadmap Disambiguation]]."
        )

    canonical_ref = links[0]
    alias_ref = ""
    disambiguation_ref = links[1]
    if len(links) >= 3:
        alias_ref = links[1]
        disambiguation_ref = links[2]

    resolution_target = alias_ref or _infer_disambiguation_subject(disambiguation_ref)
    block_lines = [f"Canonical target for [[{resolution_target}]]: [[{canonical_ref}]]."]
    if alias_ref:
        block_lines.append(f"Requested source reference: [[{alias_ref}]].")
    block_lines.append("- [x] Canonical target selected.")
    append_result = vault_edit.append_under_heading(
        disambiguation_ref,
        "Resolution",
        "\n".join(block_lines),
    )
    if not append_result.get("ok"):
        return _vault_error_text(append_result, "Could not record the canonical target.")
    return (
        f"Marked [[{canonical_ref}]] as the canonical target in {append_result['path']} "
        f"under {append_result['heading']}."
    )


def _infer_disambiguation_subject(note_ref: str) -> str:
    text = re.sub(r"^\[\[|\]\]$", "", (note_ref or "").strip())
    stem = Path(text).stem
    if stem.lower().endswith(" disambiguation"):
        return stem[: -len(" Disambiguation")].strip()
    return stem.strip() or text.strip()


def _add_agent_inbox_item(task: str) -> str | None:
    match = re.search(
        r"\b(?:add|queue|log|capture)\b(?:\s+this)?\s+(?:to\s+)?(?:the\s+)?agent\s+inbox\b\s*:?\s+(.+)$",
        task,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return (
            "To add an agent inbox item, tell me exactly what to queue, "
            "for example: Add to agent inbox: distill recent bugfix learnings into the roadmap."
        )
    item_text = match.group(1).strip()
    result = vault_capture.add_agent_inbox_item(item_text)
    if not result.get("ok"):
        return _vault_error_text(result, "Could not add that item to the agent inbox.")
    return f"Queued agent inbox item in {result['path']} under {result['heading']}."


def _promote_candidate_note(task: str) -> str | None:
    links = _extract_wikilinks(task)
    if not links:
        return (
            "To promote a candidate note, tell me the candidate note and optionally the canonical note, "
            "for example: Promote [[Curated Identity Candidate]] into [[Curated Identity]] under Notes."
        )

    candidate_ref = links[0]
    canonical_ref = links[1] if len(links) >= 2 else None
    heading_match = re.search(r"\bunder\s+([^:.]+)", task, flags=re.IGNORECASE)
    heading = heading_match.group(1).strip() if heading_match else None
    if heading and "archive" in heading.lower():
        heading = re.split(r"\s+and\s+archive\b", heading, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    result = vault_edit.promote_candidate_update(candidate_ref, canonical_ref=canonical_ref, heading=heading)
    if not result.get("ok"):
        return _vault_error_text(result, "Could not promote that candidate note.")
    if "archive" in task.lower():
        archived = vault_edit.archive_candidate_note(candidate_ref)
        if not archived.get("ok"):
            return _vault_error_text(archived, "Promoted the candidate note, but could not archive it.")
        return (
            f"Promoted [[{Path(result['candidate_path']).stem}]] into {result['canonical_path']} "
            f"under {result['heading']}, then archived it."
        )
    return (
        f"Promoted [[{Path(result['candidate_path']).stem}]] into {result['canonical_path']} "
        f"under {result['heading']}."
    )


def _review_stale_vault_work(task: str) -> str | None:
    lower = (task or "").lower()
    day_match = re.search(r"\b(\d+)\s+days?\b", lower)
    threshold = int(day_match.group(1)) if day_match else 3

    if "stale vault work" in lower or "stale vault items" in lower:
        candidate_result = vault_edit.review_stale_candidate_notes(max_age_days=threshold)
        if not candidate_result.get("ok"):
            return _vault_error_text(candidate_result, "Could not review stale candidate notes.")
        inbox_result = vault_edit.review_stale_agent_inbox(max_age_days=threshold)
        if not inbox_result.get("ok"):
            return _vault_error_text(inbox_result, "Could not review stale agent inbox items.")
        candidate_items = candidate_result.get("items") or []
        inbox_items = inbox_result.get("items") or []
        if not candidate_items and not inbox_items:
            return f"No stale vault work older than {threshold} days."
        lines = [f"Stale vault work older than {threshold} days:"]
        if candidate_items:
            lines.append("Candidates:")
            lines.extend(
                f"- [[{Path(item['path']).stem}]] -> `{item['canonical_target']}` "
                f"({item['age_days']}d, next: {item.get('recommendation', 'review')}, do: {_candidate_action_hint(item)})"
                for item in candidate_items[:4]
            )
        if inbox_items:
            lines.append("Inbox:")
            lines.extend(
                f"- {item['heading']}: {item['text']} "
                f"(due {item['due']}, {item['age_days']}d, next: {item.get('recommendation', 'review')}, do: {_inbox_action_hint(item)})"
                for item in inbox_items[:4]
            )
        return "\n".join(lines)

    if "stale candidate" in lower:
        result = vault_edit.review_stale_candidate_notes(max_age_days=threshold)
        if not result.get("ok"):
            return _vault_error_text(result, "Could not review stale candidate notes.")
        items = result.get("items") or []
        if not items:
            return f"No stale candidate notes older than {threshold} days."
        lines = [
            f"Stale candidate notes older than {threshold} days:",
            *[
                f"- [[{Path(item['path']).stem}]] -> `{item['canonical_target']}` "
                f"({item['age_days']}d, next: {item.get('recommendation', 'review')}, do: {_candidate_action_hint(item)})"
                for item in items[:6]
            ],
        ]
        return "\n".join(lines)

    if "stale inbox" in lower:
        result = vault_edit.review_stale_agent_inbox(max_age_days=threshold)
        if not result.get("ok"):
            return _vault_error_text(result, "Could not review stale agent inbox items.")
        items = result.get("items") or []
        if not items:
            return f"No stale agent inbox items older than {threshold} days."
        lines = [
            f"Stale agent inbox items older than {threshold} days:",
            *[
                f"- {item['heading']}: {item['text']} "
                f"(due {item['due']}, {item['age_days']}d, next: {item.get('recommendation', 'review')}, do: {_inbox_action_hint(item)})"
                for item in items[:6]
            ],
        ]
        return "\n".join(lines)

    return None


def _candidate_action_hint(item: dict) -> str:
    title = Path((item or {}).get("path", "")).stem or "Candidate"
    canonical_stem = Path((item or {}).get("canonical_target", "")).stem or "Canonical Note"
    recommendation = (item or {}).get("recommendation", "")
    if recommendation == "archive_or_close":
        return f"Archive [[{title}]]."
    if recommendation in {"promote_or_refresh", "review_or_archive"}:
        return f"Promote [[{title}]] into [[{canonical_stem}]]."
    return f"Review [[{title}]]."


def _inbox_action_hint(item: dict) -> str:
    text = (item or {}).get("text", "").strip()
    recommendation = (item or {}).get("recommendation", "")
    due = (item or {}).get("due", "").strip()
    if recommendation in {"archive_or_close", "promote_or_close"}:
        return f"Close inbox item: {text}"
    if recommendation == "requeue_or_split":
        return f"Requeue inbox item: {text} for {due}" if due else f"Requeue inbox item: {text}"
    return f"Close inbox item: {text}"


def _apply_recommended_action(task: str) -> str | None:
    links = _extract_wikilinks(task)
    lower = (task or "").lower()
    day_match = re.search(r"\b(\d+)\s+days?\b", lower)
    threshold = int(day_match.group(1)) if day_match else 3

    if links:
        note_ref = links[0]
        candidate_result = vault_edit.review_stale_candidate_notes(max_age_days=threshold)
        if not candidate_result.get("ok"):
            return _vault_error_text(candidate_result, "Could not inspect stale candidate recommendations.")
        note_stem = Path(note_ref).stem.lower()
        for item in candidate_result.get("items") or []:
            if Path(item.get("path", "")).stem.lower() != note_stem:
                continue
            recommendation = item.get("recommendation", "")
            if recommendation in {"archive_or_close", "review_or_archive"}:
                archived = vault_edit.archive_candidate_note(note_ref)
                if not archived.get("ok"):
                    return _vault_error_text(archived, "Could not archive that candidate note.")
                return f"Applied recommended action for [[{Path(archived['path']).stem}]]: archived."
            promoted = vault_edit.promote_candidate_update(note_ref)
            if not promoted.get("ok"):
                return _vault_error_text(promoted, "Could not promote that candidate note.")
            return f"Applied recommended action for [[{Path(promoted['candidate_path']).stem}]]: promoted into {promoted['canonical_path']}."
        return f"No stale candidate recommendation found for [[{Path(note_ref).stem}]] older than {threshold} days."

    item_match = re.search(r"\bapply recommended action(?: for)?\s*:?\s+(.+)$", task.strip(), flags=re.IGNORECASE | re.DOTALL)
    if not item_match:
        return None
    item_text = item_match.group(1).strip()
    inbox_result = vault_edit.review_stale_agent_inbox(max_age_days=threshold)
    if not inbox_result.get("ok"):
        return _vault_error_text(inbox_result, "Could not inspect stale inbox recommendations.")
    for item in inbox_result.get("items") or []:
        if item.get("text", "").strip() != item_text:
            continue
        recommendation = item.get("recommendation", "")
        if recommendation in {"archive_or_close", "promote_or_close"}:
            closed = vault_edit.close_agent_inbox_item(item_text)
            if not closed.get("ok"):
                return _vault_error_text(closed, "Could not close that inbox item.")
            return f"Applied recommended action for inbox item '{item_text}': closed."
        due = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
        requeued = vault_edit.requeue_agent_inbox_item(item_text, due_date=due)
        if not requeued.get("ok"):
            return _vault_error_text(requeued, "Could not requeue that inbox item.")
        return f"Applied recommended action for inbox item '{item_text}': requeued for {due}."
    return f"No stale inbox recommendation found for '{item_text}' older than {threshold} days."


def _apply_recommended_actions_batch(task: str) -> str | None:
    lower = (task or "").lower()
    day_match = re.search(r"\b(\d+)\s+days?\b", lower)
    threshold = int(day_match.group(1)) if day_match else 7
    cap_match = re.search(r"\b(?:cap|limit|max)\s+(\d+)\b", lower)
    cap = int(cap_match.group(1)) if cap_match else 5
    result = vault_edit.apply_recommended_actions_for_stale_vault_work(max_age_days=threshold, max_items=cap)
    if not result.get("ok"):
        return _vault_error_text(result, "Could not apply batch maintenance actions.")

    applied = result.get("applied") or []
    skipped = result.get("skipped") or []
    if not applied:
        return (
            f"No low-risk batch actions were applied for stale vault work older than {threshold} days. "
            f"Skipped {len(skipped)} item(s) that still need manual review."
        )

    lines = [
        f"Applied {len(applied)} low-risk maintenance action(s) for stale vault work older than {threshold} days (cap {result.get('max_items', cap)})."
    ]
    for item in applied[:cap]:
        lines.append(f"- {item.get('kind')}: {item.get('title')} -> {item.get('action')}")
    if skipped:
        lines.append(f"Skipped {len(skipped)} item(s) that still need manual review or exceeded the cap.")
    return "\n".join(lines)


def _maintenance_status(task: str) -> str | None:
    lower = (task or "").lower()
    day_match = re.search(r"\b(\d+)\s+days?\b", lower)
    threshold = int(day_match.group(1)) if day_match else 3
    result = vault_edit.maintenance_status(stale_after_days=threshold)
    if not result.get("ok"):
        return _vault_error_text(result, "Could not build vault maintenance status.")
    candidates = result.get("candidates", {})
    inbox = result.get("agent_inbox", {})
    return (
        f"Vault maintenance status: candidates active={candidates.get('active', 0)}, "
        f"archived={candidates.get('archived', 0)}, stale={candidates.get('stale', 0)}. "
        f"Agent inbox queued={inbox.get('queued', 0)}, in_review={inbox.get('in_review', 0)}, "
        f"done={inbox.get('done', 0)}, stale={inbox.get('stale', 0)}."
    )


def _refresh_maintenance_dashboard(task: str) -> str | None:
    lower = (task or "").lower()
    day_match = re.search(r"\b(\d+)\s+days?\b", lower)
    threshold = int(day_match.group(1)) if day_match else 3
    result = vault_edit.refresh_maintenance_dashboard(stale_after_days=threshold)
    if not result.get("ok"):
        return _vault_error_text(result, "Could not refresh the maintenance dashboard.")
    return f"Refreshed vault maintenance dashboard at {result['path']}."


def _archive_candidate_note(task: str) -> str | None:
    note_ref = _extract_wikilink(task)
    if not note_ref:
        return "To archive a candidate note, tell me which candidate note to archive, for example: Archive [[Curated Identity Candidate]]."
    result = vault_edit.archive_candidate_note(note_ref)
    if not result.get("ok"):
        return _vault_error_text(result, "Could not archive that candidate note.")
    return f"Archived [[{Path(result['path']).stem}]]."


def _apply_inbox_item_action(task: str) -> str | None:
    requeue_match = re.search(
        r"\brequeue\s+inbox\s+item\b\s*:?\s+(.+?)(?:\s+for\s+(\d{4}-\d{2}-\d{2}))?$",
        task.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if requeue_match:
        item_text = requeue_match.group(1).strip()
        due_date = (requeue_match.group(2) or "").strip() or None
        result = vault_edit.requeue_agent_inbox_item(item_text, due_date=due_date)
        if not result.get("ok"):
            return _vault_error_text(result, "Could not requeue that inbox item.")
        due_suffix = f" for {due_date}" if due_date else ""
        return f"Requeued inbox item '{item_text}'{due_suffix}."

    close_match = re.search(
        r"\bclose\s+inbox\s+item\b\s*:?\s+(.+)$",
        task.strip(),
        flags=re.IGNORECASE | re.DOTALL,
    )
    if close_match:
        item_text = close_match.group(1).strip()
        result = vault_edit.close_agent_inbox_item(item_text)
        if not result.get("ok"):
            return _vault_error_text(result, "Could not close that inbox item.")
        return f"Closed inbox item '{item_text}'."

    return None
