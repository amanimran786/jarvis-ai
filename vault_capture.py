"""
Vault capture pipeline for Jarvis.

Converts natural-language capture requests into targeted vault mutations,
following the Brain Schema (03) and Capture Workflow (04).

Responsibilities:
  - Detect capture intent from user input (tasks, decisions, notes, stories)
  - Route to the correct note/heading via vault_edit
  - Keep frontmatter `updated` and `version` current
  - Append major structural changes to the Vault Changelog
  - Never invent content — write only what was explicitly stated

Integration points:
  - router.py  — fast-path for unambiguous capture commands
  - model_router.py — optional post-response auto-capture (call log_conversation_to_vault)
  - specialized_agent_native.py — vault_curator role also calls vault_edit directly
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import vault_edit
import vault

# ── well-known note references ────────────────────────────────────────────────
TASK_HUB        = "90 Task Hub"
DECISION_LOG    = "70 Jarvis Decision Log"
CHANGELOG       = "91 Vault Changelog"
STORY_BANK      = "60 Interview Story Bank"
PROJECTS        = "20 Projects"
IDENTITY        = "10 Identity"
PREFERENCES     = "30 Preferences"
SYNTHESIS       = "50 Synthesis"

# Default headings inside notes
_TASK_HEADING       = "Incoming"
_DECISION_HEADING   = "Decisions"
_CHANGELOG_HEADING  = None          # built dynamically per date
_STORY_HEADING      = "Stories"
_PROJECT_HEADING    = "Recent Updates"


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _patch_frontmatter(path: Path) -> None:
    """Bump `updated` date and increment `version` in YAML frontmatter."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return
    today = _today()
    raw = re.sub(r"^(updated:\s*).*$", rf"\g<1>{today}", raw, flags=re.MULTILINE)
    raw = re.sub(
        r"^(version:\s*)(\d+)$",
        lambda m: f"{m.group(1)}{int(m.group(2)) + 1}",
        raw,
        flags=re.MULTILINE,
    )
    try:
        path.write_text(raw, encoding="utf-8")
    except OSError:
        pass


def _resolve_and_patch(note_ref: str) -> None:
    """After a successful write, bump frontmatter on the note."""
    resolved = vault_edit.resolve_note_path(note_ref)
    if resolved:
        _patch_frontmatter(resolved)


# ── public capture functions ──────────────────────────────────────────────────

def add_task(task_text: str, date: str | None = None, tag: str = "#brain") -> dict:
    """
    Append a plain-markdown task to the Task Hub under the Incoming heading.

    Args:
        task_text: Human-readable task description (no leading checkbox needed).
        date:      Optional due date string YYYY-MM-DD (defaults to today).
        tag:       Obsidian tag for the task (default #brain).

    Returns:
        vault_edit result dict.
    """
    task_text = (task_text or "").strip().lstrip("-[] ")
    if not task_text:
        return {"ok": False, "error": "No task text provided."}
    due = date or _today()
    block = f"- [ ] {task_text} 📅 {due} {tag}"
    result = vault_edit.append_under_heading(TASK_HUB, _TASK_HEADING, block)
    if result.get("ok"):
        _resolve_and_patch(TASK_HUB)
    return result


def log_decision(
    title: str,
    decision: str,
    why: str,
    tradeoffs: str = "",
    affected: str = "",
) -> dict:
    """
    Append a structured decision entry to the Decision Log.

    All fields are required except tradeoffs and affected.
    """
    title = (title or "").strip()
    decision = (decision or "").strip()
    why = (why or "").strip()
    if not title or not decision or not why:
        return {"ok": False, "error": "Decision needs a title, decision statement, and reason."}
    today = _today()
    lines = [
        f"### {title}",
        f"Date: {today}",
        "",
        f"**Decision:** {decision}",
        "",
        f"**Why:** {why}",
    ]
    if tradeoffs:
        lines += ["", f"**Tradeoffs:** {tradeoffs.strip()}"]
    if affected:
        lines += ["", f"**Affected:** {affected.strip()}"]
    lines += ["", "---"]
    block = "\n".join(lines)
    result = vault_edit.append_under_heading(DECISION_LOG, _DECISION_HEADING, block)
    if result.get("ok"):
        _resolve_and_patch(DECISION_LOG)
    return result


def add_changelog_entry(summary: str, note_refs: list[str] | None = None) -> dict:
    """
    Append a dated changelog entry to the Vault Changelog.

    Args:
        summary:   What changed and why (plain prose or bullet list).
        note_refs: Wikilinks to affected notes, e.g. ["[[20 Projects]]"].
    """
    summary = (summary or "").strip()
    if not summary:
        return {"ok": False, "error": "No changelog summary provided."}
    today = _today()
    heading = today                         # dated heading, e.g. "2026-04-15"
    lines = [summary]
    if note_refs:
        links = " · ".join(f"[[{r.strip().strip('[').strip(']')}]]" for r in note_refs)
        lines += ["", f"Affected: {links}"]
    block = "\n".join(lines)
    result = vault_edit.append_under_heading(CHANGELOG, heading, block)
    if result.get("ok"):
        _resolve_and_patch(CHANGELOG)
    return result


def capture_story(
    title: str,
    situation: str,
    task: str,
    action: str,
    result_text: str,
    role_targets: list[str] | None = None,
) -> dict:
    """
    Append a STAR-format story entry to the Interview Story Bank.
    """
    title = (title or "").strip()
    if not title:
        return {"ok": False, "error": "Story needs a title."}
    lines = [
        f"### {title}",
        "",
        f"**Situation:** {situation.strip()}",
        "",
        f"**Task:** {task.strip()}",
        "",
        f"**Action:** {action.strip()}",
        "",
        f"**Result:** {result_text.strip()}",
    ]
    if role_targets:
        targets = ", ".join(role_targets)
        lines += ["", f"**Role targets:** {targets}"]
    lines += ["", "---"]
    block = "\n".join(lines)
    result = vault_edit.append_under_heading(STORY_BANK, _STORY_HEADING, block)
    if result.get("ok"):
        _resolve_and_patch(STORY_BANK)
    return result


def update_projects(update_text: str) -> dict:
    """
    Append a quick project update to 20 Projects under Recent Updates.
    """
    update_text = (update_text or "").strip()
    if not update_text:
        return {"ok": False, "error": "No update text provided."}
    today = _today()
    block = f"- {today}: {update_text}"
    result = vault_edit.append_under_heading(PROJECTS, _PROJECT_HEADING, block)
    if result.get("ok"):
        _resolve_and_patch(PROJECTS)
    return result


def save_to_brain(content: str, title: str, area: str = "vault") -> dict:
    """
    Create a new brain note from template.

    If a note with that title already exists, append content to it instead.
    """
    title = (title or "").strip()
    content = (content or "").strip()
    if not title:
        return {"ok": False, "error": "Note needs a title."}

    # Try to append to existing note first
    existing = vault_edit.resolve_note_path(title)
    if existing:
        result = vault_edit.append_under_heading(title, "Notes", content)
        if result.get("ok"):
            _resolve_and_patch(title)
        return result

    # Create new note from template
    create_result = vault_edit.create_note_from_template(title, "brain-note-template")
    if not create_result.get("ok"):
        return create_result

    # Now append the content under the Notes heading
    append_result = vault_edit.append_under_heading(title, "What This Note Holds", content)
    if append_result.get("ok"):
        _resolve_and_patch(title)
        return {**create_result, **append_result, "action": "created_and_populated"}
    return create_result


def append_to_note(note_ref: str, heading: str, content: str) -> dict:
    """
    Generic: append content under a specific heading in any vault note.
    Thin wrapper over vault_edit that also bumps frontmatter.
    """
    result = vault_edit.append_under_heading(note_ref, heading, content)
    if result.get("ok"):
        _resolve_and_patch(note_ref)
    return result


def read_note(note_ref: str, max_chars: int = 1800) -> dict:
    """Thin pass-through to vault_edit.read_note."""
    return vault_edit.read_note(note_ref, max_chars=max_chars)


# ── intent detection ──────────────────────────────────────────────────────────

_TASK_RE = re.compile(
    r"\b(?:add\s+(?:a\s+)?task|new\s+task|capture\s+task|todo|to-do|to\s+do|remind\s+me\s+to|add\s+(?:this\s+)?to\s+(?:the\s+)?task(?:\s+hub)?)\b",
    re.IGNORECASE,
)
_DECISION_RE = re.compile(
    r"\b(?:log|record|save|capture)\s+(?:a\s+)?decision\b"
    r"|\bdecision:\s"
    r"|\bwe\s+decided\b"
    r"|\bi\s+decided\b"
    r"|\bdecided\s+to\b"
    r"|\barchitecture\s+decision\b",
    re.IGNORECASE,
)
_CHANGELOG_RE = re.compile(
    r"\b(?:update|add|log)\s+(?:(?:the\s+)?vault\s+)?changelog\b"
    r"|\bchangelog\s+entry\b",
    re.IGNORECASE,
)
_STORY_RE = re.compile(
    r"\b(?:save|capture|add)\s+(?:a\s+)?(?:interview\s+)?story\b"
    r"|\b(?:save|add)\s+(?:to\s+)?(?:the\s+)?story\s+bank\b"
    r"|\bstar\s+(?:story|format)\b",
    re.IGNORECASE,
)
_BRAIN_SAVE_RE = re.compile(
    r"\b(?:save|add|capture|write)\s+(?:this\s+)?to\s+(?:the\s+)?(?:vault|brain|obsidian)\b"
    r"|\b(?:save|capture)\s+(?:this\s+)?(?:as\s+a?\s+)?(?:brain\s+)?note\b"
    r"|\bvault\s+this\b"
    r"|\badd\s+to\s+brain\b",
    re.IGNORECASE,
)
_PROJECT_UPDATE_RE = re.compile(
    r"\bupdate\s+(?:the\s+)?project(?:s)?\b"
    r"|\badd\s+(?:a\s+)?project\s+update\b"
    r"|\bproject\s+update\s*:",
    re.IGNORECASE,
)
_APPEND_NOTE_RE = re.compile(
    r"\b(?:append|add|update)\s+(?:to\s+)?\[\[",
    re.IGNORECASE,
)
_READ_NOTE_RE = re.compile(
    r"\b(?:read|show|open)\s+(?:(?:brain\s+|vault\s+)?note\s+)?\[\["
    r"|\bwhat(?:'?s)?\s+in\s+\[\[",
    re.IGNORECASE,
)


def detect_capture_intent(user_input: str) -> str | None:
    """
    Return the capture intent string if the input is a clear capture command,
    else None.

    Possible return values:
      "task", "decision", "changelog", "story", "brain_save",
      "project_update", "append_note", "read_note"
    """
    if not user_input:
        return None
    if _TASK_RE.search(user_input):
        return "task"
    if _DECISION_RE.search(user_input):
        return "decision"
    if _CHANGELOG_RE.search(user_input):
        return "changelog"
    if _STORY_RE.search(user_input):
        return "story"
    if _BRAIN_SAVE_RE.search(user_input):
        return "brain_save"
    if _PROJECT_UPDATE_RE.search(user_input):
        return "project_update"
    if _APPEND_NOTE_RE.search(user_input):
        return "append_note"
    if _READ_NOTE_RE.search(user_input):
        return "read_note"
    return None


# ── natural-language dispatch ─────────────────────────────────────────────────

def _extract_after_colon(text: str) -> str:
    """Return everything after the first colon, stripped."""
    idx = text.find(":")
    return text[idx + 1:].strip() if idx >= 0 else text.strip()


def _extract_wikilink(text: str) -> str:
    match = re.search(r"\[\[([^\]]+)\]\]", text)
    return match.group(1).strip() if match else ""


def handle_capture(user_input: str) -> str | None:
    """
    Attempt to handle a natural-language capture command entirely without LLM.

    Returns a confirmation string on success, None if the input doesn't
    match any capture pattern (caller should fall through to smart_stream).
    """
    intent = detect_capture_intent(user_input)
    if intent is None:
        return None

    # ── task ──────────────────────────────────────────────────────────────────
    if intent == "task":
        task_text = _extract_after_colon(user_input)
        # Strip the trigger phrase
        task_text = re.sub(
            r"^(?:add\s+(?:a\s+)?task|new\s+task|capture\s+task|todo|to-do|to\s+do|remind\s+me\s+to|add\s+(?:this\s+)?to\s+(?:the\s+)?task(?:\s+hub)?)[\s:,]+",
            "",
            task_text,
            flags=re.IGNORECASE,
        ).strip()
        if not task_text:
            return None     # not enough info — let LLM handle it
        result = add_task(task_text)
        if result.get("ok"):
            return f'Added task to vault: "{task_text}"'
        return f"Couldn't add task: {result.get('error', 'unknown error')}"

    # ── decision (requires structured fields — fall through to vault_curator) ─
    if intent == "decision":
        return None     # Let orchestrator route to vault_curator for richer extraction

    # ── changelog ─────────────────────────────────────────────────────────────
    if intent == "changelog":
        summary = _extract_after_colon(user_input)
        summary = re.sub(
            r"^(?:update|add|log)\s+(?:(?:the\s+)?vault\s+)?changelog\s*",
            "",
            summary,
            flags=re.IGNORECASE,
        ).strip()
        if not summary:
            return None
        result = add_changelog_entry(summary)
        if result.get("ok"):
            return f"Added changelog entry for {_today()}."
        return f"Couldn't add changelog entry: {result.get('error', 'unknown error')}"

    # ── story (requires STAR fields — fall through) ────────────────────────────
    if intent == "story":
        return None     # Let vault_curator extract STAR fields

    # ── brain save ────────────────────────────────────────────────────────────
    if intent == "brain_save":
        # "save to vault: <content> as <title>"  or just  "save to vault: <content>"
        body = _extract_after_colon(user_input)
        body = re.sub(
            r"^(?:save|add|capture|write)\s+(?:this\s+)?to\s+(?:the\s+)?(?:vault|brain|obsidian)\b[\s:,]*",
            "",
            body,
            flags=re.IGNORECASE,
        ).strip()
        if not body:
            return None
        title_match = re.search(r"\bas\s+(?:(?:a\s+)?(?:note\s+)?(?:called|titled|named|title)\s+)?[\"']?(.+?)[\"']?\s*$", body, re.IGNORECASE)
        if title_match:
            title = title_match.group(1).strip()
            content = body[: title_match.start()].strip()
        else:
            # Auto-title from first sentence
            first_sentence = re.split(r"[.!?\n]", body)[0][:60].strip()
            title = first_sentence if first_sentence else "Captured Note"
            content = body
        result = save_to_brain(content, title)
        if result.get("ok"):
            return f'Saved to vault: "{title}" at {result.get("path", "vault/wiki/brain/")}.'
        return f"Couldn't save to vault: {result.get('error', 'unknown error')}"

    # ── project update ────────────────────────────────────────────────────────
    if intent == "project_update":
        update = _extract_after_colon(user_input)
        update = re.sub(
            r"^(?:update\s+(?:the\s+)?project(?:s)?|add\s+(?:a\s+)?project\s+update|project\s+update)\s*:?\s*",
            "",
            update,
            flags=re.IGNORECASE,
        ).strip()
        if not update:
            return None
        result = update_projects(update)
        if result.get("ok"):
            return f"Project update logged to vault."
        return f"Couldn't log project update: {result.get('error', 'unknown error')}"

    # ── append to explicit wikilink note ─────────────────────────────────────
    if intent == "append_note":
        note_ref = _extract_wikilink(user_input)
        if not note_ref:
            return None
        heading_match = re.search(r"\bunder\s+[\"']?([^\"':]+)[\"']?\s*:", user_input, re.IGNORECASE)
        heading = heading_match.group(1).strip() if heading_match else "Notes"
        # Content is everything after the last colon
        content = _extract_after_colon(user_input)
        if not content:
            return None
        result = append_to_note(note_ref, heading, content)
        if result.get("ok"):
            return f'Appended to [[{note_ref}]] under "{result["heading"]}".'
        return f"Couldn't append to note: {result.get('error', 'unknown error')}"

    # ── read note ─────────────────────────────────────────────────────────────
    if intent == "read_note":
        note_ref = _extract_wikilink(user_input)
        if not note_ref:
            return None
        result = read_note(note_ref)
        if result.get("ok"):
            content = result.get("content", "").strip()
            truncated = " (truncated)" if result.get("truncated") else ""
            return f"**{result['title']}** ({result['path']}){truncated}:\n\n{content}"
        return f"Couldn't read note: {result.get('error', 'unknown error')}"

    return None


# ── changelog helper for major integration milestones ─────────────────────────

def record_integration_milestone(description: str, affected_files: list[str] | None = None) -> None:
    """
    Silently log a major Jarvis integration event to the Vault Changelog.
    Called by other modules when a significant brain change lands.
    Does not raise — failures are suppressed.
    """
    try:
        add_changelog_entry(description, note_refs=affected_files)
    except Exception:
        pass
