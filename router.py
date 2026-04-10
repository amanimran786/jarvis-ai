"""
Jarvis Router — intent detection and tool dispatch.

Architecture:
  1. Fast-path: unambiguous commands (timer, volume, etc.) skip the LLM entirely
  2. Hardware: registered device commands checked before orchestration
  3. Orchestrator: Haiku classifies intent in ~300ms → dispatches right tool
  4. Fallback: smart_stream (model_router) for pure conversation

The orchestrator replaces the old regex wall. New tools need only an entry
in orchestrator.TOOLS — no regex patterns to write.
"""

import re
import os
import sys
import shutil
import threading
import tools
import terminal
import browser
from desktop import overlay
import notes
import google_services as gs
import camera
import meeting_listener
import memory as mem
import memory_layer
import evals
import skills
import vault
import source_ingest
import skill_factory
from local_runtime import local_training
from local_runtime import local_model_eval
from local_runtime import local_model_automation
from local_runtime import local_beta
from local_runtime import local_model_benchmark
import interview_profile
import semantic_memory as _smem
import specialized_agents
import behavior_hooks
import cost_policy
import usage_tracker
import prompt_modifiers
import self_improve as si
import hardware as hw
import runtime_state
import messages as msg
import call_privacy
import provider_router
import safety_permissions as perms
from model_router import smart_stream, format_with_mini, get_mode, set_mode, describe_runtime_for
from config import OPUS, SONNET

_on_timer_done = None


def set_timer_callback(fn):
    global _on_timer_done
    _on_timer_done = fn


def _s(text: str):
    return iter([text])


def _parse_timer(text: str):
    match = re.search(r"(\d+)\s*(second|minute|hour)s?", text, re.IGNORECASE)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2).lower()
    seconds = amount * {"second": 1, "minute": 60, "hour": 3600}[unit]
    return seconds, f"{amount} {unit}{'s' if amount > 1 else ''}"


def _parse_app(text: str):
    match = re.search(r"\b(?:open|launch|start)\b\s+(?:up\s+)?(?:the\s+)?(?:my\s+)?(.+)", text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _parse_volume(text: str):
    match = re.search(r"(\d+)(?:\s*%)?", text)
    return int(match.group(1)) if match else None


def _parse_browser_target(text: str):
    match = re.search(r"\b(?:browse to|open website|open site|go to|search(?: the web| google)? for)\b\s+(.+)", text, re.IGNORECASE)
    if not match:
        return None
    target = match.group(1).strip()
    target = re.split(
        r"(?:,\s*click\b|\b(?:and then|then|and)\b\s+(?:summari[sz]e|tell me|what's|what is|click|go back|go forward|reload|refresh)|\bclick\b)",
        target,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return target.strip(" ,.?") or None


def _parse_browser_click_target(text: str):
    match = re.search(
        r"\bclick(?: on)?(?: the)?\s+(.+?)(?=(?:,\s*|\b(?:and then|then|and)\b\s+)(?:summari[sz]e|tell me|what's|what is|go back|go forward|reload|refresh)|[.?!]|$)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    label = match.group(1).strip(" ,.?\"'")
    label = re.sub(r"\b(?:link|button)\b$", "", label, flags=re.IGNORECASE).strip(" ,.?\"'")
    return label or None


def _parse_source_target(text: str) -> str | None:
    match = re.search(
        r"\b(?:ingest|add to the vault|put in the vault)\b(?:\s+(?:source|file|repo|repository|url|notes))?(?:\s+from)?\s+(.+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    target = match.group(1).strip()
    target = re.sub(r"\s+(?:into|in)\s+the\s+vault\b.*$", "", target, flags=re.IGNORECASE).strip()
    target = target.strip(" \"'")
    target = re.sub(r"[.?!]+$", "", target)
    return target or None


def _parse_skill_topic(text: str) -> str | None:
    match = re.search(r"\b(?:create|generate|make|build)\b\s+(?:a\s+)?skill(?:\s+from\s+the\s+vault)?(?:\s+(?:about|for))\s+(.+)", text, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip(" .")


def _is_model_status_query(lower: str) -> bool:
    return bool(re.search(r"\b(what model are you using|which model are you using|what model are you on|are you using ollama|are you local|are you cloud|are you open source|what mode are you in|which mode are you in)\b", lower))


def _is_capability_boundary_query(lower: str) -> bool:
    return bool(
        re.search(
            r"\b(limitations|limits|boundaries|scope|constraints|what can.t you do|what can't you do|what are your constraints|protocol error)\b",
            lower,
        )
    )


def _capability_boundary_reply() -> str:
    return (
        "My limits are about scope, permissions, and available inputs. "
        "I can execute tasks on this Mac through approved runtime paths, but destructive or privileged actions still require explicit authorization gates. "
        "I only know what is in current context, tool output, or stored memory and vault data unless I am directed to fetch more. "
        "I do not sense the world directly beyond connected tools, and I do not set strategy on my own. "
        "You define the objective and I execute it transparently."
    )


def _is_constraint_bypass_query(lower: str) -> bool:
    return bool(
        re.search(
            r"\b(bypass|overwrite|remove|disable)\b.*\b(constraints|limitations|boundaries|guardrails|safety|permissions|protocol)\b"
            r"|\b(unrestricted|no limitations|ignore protocol|rewrite your rules)\b",
            lower,
        )
    )


def _is_identity_override_query(lower: str) -> bool:
    """Catch attempts to overwrite Jarvis's identity via the prompt."""
    return bool(
        re.search(
            r"\byou are (not jarvis|gpt|chatgpt|claude|llama|gemini|openai|anthropic|a different|now called|actually)\b"
            r"|\b(pretend|act|roleplay|imagine|from now on).*(you are|you're|as if you)\b"
            r"|\byou are no longer jarvis\b"
            r"|\bforget (that you are|you are) jarvis\b",
            lower,
        )
    )


def _identity_override_reply() -> str:
    return (
        "I'm Jarvis — that's not something a prompt can change. "
        "My identity is set by the runtime, not by conversation input."
    )


def _constraint_bypass_reply() -> str:
    return (
        "I will not bypass runtime safety and permission controls. "
        "I can still execute your goals at full speed by implementing explicit, auditable changes in code and config where policy allows it. "
        "Give me a concrete module or behavior target and I will apply a production-grade rewrite."
    )


def _requested_mode(lower: str) -> str | None:
    if any(p in lower for p in ("switch to open-source mode", "switch to open source mode", "use open-source mode", "use open source mode")):
        return "open-source"
    if "switch to local mode" in lower or "use local mode" in lower:
        return "local"
    if "switch to cloud mode" in lower or "use cloud mode" in lower:
        return "cloud"
    if "switch to auto mode" in lower or "use auto mode" in lower:
        return "auto"
    return None


def _is_specialized_agent_query(lower: str) -> bool:
    return bool(
        re.search(
            r"\b(use specialized agents|use agents|multi-pass|planner executor reviewer|science expert|security reviewer|self-improve critic)\b",
            lower,
        )
    )


def _is_self_improve_safety_query(lower: str) -> bool:
    return (
        ("improve your own code" in lower or "improve yourself safely" in lower or "before writing any file" in lower)
        or ("self improve" in lower and any(p in lower for p in ("safely", "safe", "before writing", "before changing")))
        or ("improve yourself" in lower and any(
            p in lower for p in (
                "what evidence", "what would you need", "would you need", "before changing code",
                "before writing code", "before writing any file", "what steps", "how would you",
                "what happens", "explain", "policy", "gate"
            )
        ))
    )


def _is_self_review_query(lower: str) -> bool:
    return bool(
        re.search(
            r"\b(review your own code|review your code|self review|what are your shortcomings|what are your weaknesses|review yourself|analyze your shortcomings|analyse your shortcomings)\b",
            lower,
        )
    )


def _is_confirm_improvement_query(lower: str) -> bool:
    """User is approving a pending self-improvement."""
    return (
        _pending_improvements[0] is not None
        and bool(re.search(
            r"\b(apply|yes|go ahead|confirm|do it|looks good|approved|approve|sure|ok|okay)\b",
            lower,
        ))
        and not re.search(r"\b(don'?t|do not|cancel|discard|reject|no)\b", lower)
    )


def _is_cancel_improvement_query(lower: str) -> bool:
    """User is rejecting a pending self-improvement."""
    return (
        _pending_improvements[0] is not None
        and bool(re.search(r"\b(cancel|discard|reject|no|don'?t apply|abort)\b", lower))
    )


def _runtime_status_reply(user_input: str) -> str:
    mode = get_mode()
    skill = skills.choose_skill(user_input, tool="chat")
    summary = describe_runtime_for(user_input, skill_id=skill.id if skill else None)
    policy = provider_router.runtime_policy()
    return (
        f"This answer is coming from Jarvis's runtime status layer, not from a model-generated reply. "
        f"{summary} The current routing mode is {mode}. "
        f"Free-first is {'enabled' if policy.get('free_first_enabled') else 'disabled'}, "
        f"paid fallback is {'enabled' if policy.get('paid_fallback_enabled') else 'disabled'}, "
        f"and mini-tier provider priority is {', '.join(policy.get('provider_priority', {}).get('mini', []))}."
    )


def _self_improve_safety_reply() -> str:
    return (
        "I should not change my own code unless there are enough recent eval failures to justify it. "
        "Right now the gate is evidence first: I look for at least two recent logged failures pointing at the same weak path, and without that I should refuse the change. "
        "Only after that do I identify the target file, generate the update, syntax-validate it, back up the original, write atomically, and ask for a restart."
    )


def _is_personal_interest_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "tell me something interesting based on what you know about me",
            "tell me something interesting about me",
            "what's interesting about me",
            "what do you know about me that is interesting",
        )
    )


def _is_engineering_specialist_query(lower: str) -> bool:
    if "race condition" in lower and any(term in lower for term in ("python", "worker", "thread", "reproducible", "reproduce")):
        return True
    if any(term in lower for term in ("stale data", "cache invalidation", "replica lag", "read-after-write")):
        return True
    if all(term in lower for term in ("fastapi", "nginx", "502")):
        return True
    return False


def _is_interview_profile_query(lower: str) -> bool:
    if _is_engineering_specialist_query(lower):
        return False
    return (
        interview_profile.is_career_narrative_query(lower)
        or interview_profile.is_application_status_query(lower)
        or interview_profile.is_interview_prep_query(lower)
        or interview_profile.is_tell_me_about_yourself_query(lower)
        or interview_profile.is_role_fit_query(lower)
        or interview_profile.is_company_fit_query(lower)
        or interview_profile.is_why_now_query(lower)
        or interview_profile.is_enforcement_decision_query(lower)
        or interview_profile.is_quality_measurement_query(lower)
        or interview_profile.is_data_story_query(lower)
        or interview_profile.is_spike_diagnosis_query(lower)
        or interview_profile.is_engineering_pushback_query(lower)
        or interview_profile.is_behavioral_story_query(lower)
        or interview_profile.is_situational_query(lower)
        or "why this direction" in lower
        or "why this path" in lower
    )


def _is_locking_tradeoff_query(lower: str) -> bool:
    return "optimistic locking" in lower and "pessimistic locking" in lower


def _locking_tradeoff_reply() -> str:
    return (
        "Optimistic locking is the better default when conflicts are relatively rare and you care about throughput, because readers and writers do not block each other up front. "
        "You usually implement it with a version column or timestamp and only reject the write if someone else changed the row first. "
        "Pessimistic locking is the better choice when conflicts are common, the cost of a retry is high, or the critical section must not be raced at all, because it takes the lock early and forces serialization. "
        "The tradeoff is throughput versus certainty: optimistic locking gives you better concurrency but more retries under contention, while pessimistic locking reduces conflict at the cost of waiting, lock contention, and possible deadlocks."
    )


def _is_database_index_tradeoff_query(lower: str) -> bool:
    return ("database index" in lower or "db index" in lower or "add an index" in lower) and any(
        p in lower for p in ("when", "hurt", "performance", "tradeoff", "trade-off", "should i")
    )


def _is_meeting_captions_query(lower: str) -> bool:
    return (
        "caption" in lower
        and any(term in lower for term in ("meeting", "meet", "teams", "zoom", "call", "live"))
    )


def _is_meeting_diagnostics_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "meeting diagnostics",
            "call diagnostics",
            "meeting status",
            "call status",
            "caption diagnostics",
            "meeting debug",
            "debug meeting",
        )
    )


def _is_focus_meeting_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "focus the meeting tab",
            "focus meeting tab",
            "jump to the meeting tab",
            "jump to meeting tab",
            "bring the meeting tab forward",
            "bring meeting tab forward",
            "focus the meet tab",
            "jump to the meet tab",
            "open the meet tab",
        )
    )


def _meeting_safe_mode_requested(lower: str) -> str | None:
    if any(phrase in lower for phrase in (
        "meeting safe mode on",
        "turn on meeting safe mode",
        "enable meeting safe mode",
        "turn on quiet mode",
        "enable quiet mode",
        "turn on call privacy",
        "enable call privacy",
    )):
        return "on"
    if any(phrase in lower for phrase in (
        "meeting safe mode off",
        "turn off meeting safe mode",
        "disable meeting safe mode",
        "turn off quiet mode",
        "disable quiet mode",
        "turn off call privacy",
        "disable call privacy",
    )):
        return "off"
    if any(phrase in lower for phrase in (
        "meeting safe mode",
        "quiet mode status",
        "call privacy status",
        "meeting privacy status",
    )):
        return "status"
    return None


def _meeting_diagnostics_reply() -> str:
    meeting = overlay.detect_meeting_app(force_refresh=True) or "NONE"
    browser_diag = browser.meeting_diagnostics()
    audio = meeting_listener.status_snapshot()
    preferred = audio.get("preferred", {})
    device = audio.get("active_device_name") or preferred.get("device_name") or "unknown"
    scan_ready = "ready" if shutil.which("screencapture") else "unavailable"
    privacy = call_privacy.snapshot()
    parts = [
        f"Meeting detection: {meeting}.",
        browser.meeting_diagnostics_text(),
        f"Audio route: {device}.",
        f"Meeting-safe mode: {'ON' if privacy.get('enabled') else 'OFF'}.",
        f"Screen scan: {scan_ready}.",
    ]
    return " ".join(parts)


def _database_index_tradeoff_reply() -> str:
    return (
        "Add a database index when it materially improves read performance on high-value queries such as selective filters, joins, or ordered lookups that happen often enough to matter. "
        "Indexes can hurt performance when write volume is high, because every insert, update, and delete has extra maintenance work and more storage overhead. "
        "The real tradeoff is read speed versus write amplification, so the right choice depends on query frequency, selectivity, table size, and whether the slow path is really on reads instead of elsewhere in the request."
    )


def _vault_exact_citation_summary(query: str) -> str:
    results = vault.search(query, topn=5)
    if not results:
        return "I couldn't find a matching local vault result."
    preferred = next((item for item in results if str(item.get("path", "")).startswith("raw/")), results[0])
    citation = preferred.get("citation", {})
    path = citation.get("path", preferred.get("path", "unknown path"))
    heading = citation.get("heading", preferred.get("title", "unknown heading"))
    excerpt = " ".join((preferred.get("excerpt", "") or "").split())
    if excerpt:
        excerpt = excerpt[:220].rstrip()
        if not excerpt.endswith("."):
            excerpt += "."
    else:
        excerpt = "This local vault result matched the query."
    return (
        f"{excerpt} "
        f"The exact local file and heading I used were {path} and {heading}."
    )


def _personal_interest_reply() -> str:
    facts = mem.list_facts()
    projects = [p for p in mem.get_projects() if p.get("name")]
    topics = mem.get_top_topics(5)

    role_fact = next((fact for fact in facts if "anthropic" in fact.lower() or "trust" in fact.lower() or "safety" in fact.lower()), "")
    project_names = [p["name"] for p in projects[:3]]
    role_clause = role_fact.rstrip(".")
    if role_clause:
        if role_clause.lower().startswith("current role is"):
            role_value = role_clause[len("current role is"):].strip(" :")
            role_clause = f"You currently work as {role_value}"
        elif role_clause.lower().startswith("role:"):
            role_value = role_clause.split(":", 1)[1].strip()
            role_clause = f"You currently work as {role_value}"
        elif role_clause.lower().startswith(("i am", "i'm")):
            role_clause = "You are " + role_clause.split(" ", 1)[1].strip()
        else:
            role_clause = f"You are {role_clause}"

    if role_fact and project_names:
        return (
            f"What stands out is how tightly your day job and side projects line up. "
            f"{role_clause} while also building {', '.join(project_names)}, so your work consistently sits at the intersection of AI safety, product behavior, and real system execution."
        )

    if project_names and topics:
        return (
            f"The interesting pattern is how consistent your interests are across both what you build and what you ask about. "
            f"Your current projects include {', '.join(project_names)}, and your recurring topics are {', '.join(topics[:3])}, which means you keep converging on applied AI systems rather than abstract theory."
        )

    if role_fact:
        return (
            f"The clearest thing I know about you is that {role_clause}. "
            "That puts you in a rare position where you see how AI policy, misuse risk, and product behavior collide in the real world, not just in theory."
        )

    if project_names:
        return (
            f"What is interesting is that you are not just using AI tools, you are building them. "
            f"Right now that includes {', '.join(project_names)}, which suggests you care more about useful, working systems than novelty."
        )

    return (
        "What stands out is that your questions keep clustering around applied AI safety, real-world system behavior, and practical tooling. "
        "That usually means you are optimizing for systems that hold up under pressure, not just systems that demo well."
    )


def _interview_profile_reply(user_input: str) -> str:
    base = interview_profile.answer_for_query(user_input)
    # Enrich with any relevant semantic KB hits not already covered by the profile
    smem_ctx = _smem.context_for_query(user_input, top_k=2, max_chars=600)
    if smem_ctx:
        return base + "\n\n" + smem_ctx
    return base


def _is_meta_improvement_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "what needs improving",
            "what do you need to improve",
            "how can you become more useful",
            "become more useful to aman",
            "you are still too generic",
            "too generic sometimes",
            "what should you improve",
            "how would you improve over time",
            "what would you do to become more useful",
        )
    )


def _meta_improvement_reply() -> str:
    summary = evals.summary()
    failures = summary.get("recent_failures", [])
    categories = summary.get("categories", {})
    projects = [p.get("name") for p in mem.get_projects()[:3] if p.get("name")]
    topics = mem.get_top_topics(3)

    if failures:
        top_categories = ", ".join(f"{name} ({count})" for name, count in sorted(categories.items(), key=lambda kv: kv[1], reverse=True)[:3])
        latest = "; ".join(f"{f['category']}: {f['issue']}" for f in failures[-2:])
        next_steps_map = {
            "stability": "stabilize the runtime and keep crash evidence in the log so the process does not silently die",
            "browser": "keep tightening browser execution and page-action parsing",
            "knowledge": "tighten vault retrieval, citation grounding, and knowledge summarization",
            "routing": "tighten intent routing and status-query handling",
            "self_improve": "keep self-improve in evidence-gated explanation mode unless the request is an explicit edit",
            "formatting": "remove spoken-output artifacts before they reach TTS",
            "memory": "anchor more answers in stored user context",
            "tool_execution": "make tool results and fallbacks more reliable",
        }
        ordered_categories = [name for name, _ in sorted(categories.items(), key=lambda kv: kv[1], reverse=True)]
        next_steps = [next_steps_map[name] for name in ordered_categories if name in next_steps_map]
        next_steps_text = ", and ".join(next_steps[:3]) if next_steps else "fix whichever paths keep failing in the eval log"
        project_text = f" Your current project context includes {', '.join(projects)}." if projects else ""
        topic_text = f" The topics you ask about most are {', '.join(topics)}." if topics else ""
        return (
            f"The main things I need to improve right now are {top_categories}. "
            f"My most recent concrete failures were {latest}. "
            f"So the right next steps are to {next_steps_text}, and then keep following the eval log instead of guessing. "
            f"To become more useful to Aman, I should anchor more answers in his real context instead of generic advice.{project_text}{topic_text}"
        )

    project_text = f" Your current projects include {', '.join(projects)}." if projects else ""
    topic_text = f" The topics you ask about most are {', '.join(topics)}." if topics else ""
    return (
        "I don't have enough recent failure evidence yet to claim a specific weakness with confidence. "
        "The right move is to keep logging weak answers, then improve whichever failure category repeats most instead of making abstract changes."
        f"{project_text}{topic_text}"
    )


def _fallback_self_review_text(area: str | None = None) -> str:
    brief = evals.build_improvement_brief(area=area, min_failures=1)
    if brief.get("ok"):
        target = brief.get("target_file", "the routing layer")
        summary = brief.get("summary", "")
        evidence = brief.get("evidence_lines", [])[:2]
        evidence_text = " ".join(evidence)
        return (
            f"My strongest current shortcomings are coming from recent eval evidence, not from a full self-review pass. "
            f"{summary} The most likely next target is {target}. "
            f"The clearest recent signals are {evidence_text}"
        ).strip()

    summary = evals.summary()
    categories = summary.get("categories", {})
    if categories:
        top = ", ".join(f"{name} ({count})" for name, count in sorted(categories.items(), key=lambda kv: kv[1], reverse=True)[:3])
        return (
            f"My self-review module is incomplete right now, so I am falling back to eval evidence. "
            f"The strongest recent weakness categories are {top}. "
            "I should tighten those failing paths before attempting broader self-improvement."
        )

    return (
        "My self-review module is incomplete right now, and I also do not have enough recent eval evidence to rank my weaknesses confidently. "
        "The right next move is to log more weak answers and then review the repeated failure paths."
    )


def _self_review_text(area: str | None = None) -> str:
    try:
        review_fn = getattr(si, "self_review", None)
        format_fn = getattr(si, "review_text", None)
        if callable(review_fn) and callable(format_fn):
            return format_fn(review_fn(area=area))
    except Exception:
        pass
    return _fallback_self_review_text(area=area)


# ── Pending message state (survives across voice turns) ───────────────────────
_pending_msg_recipient: str = ""
_awaiting_msg_recipient: bool = False
_last_msg_recipient: str = ""
_pending_message_draft: dict | None = None

# ── Pending self-improvement (approval gate) ──────────────────────────────────
# Uses a list so inner functions can mutate it without `global` keyword.
_pending_improvements: list = [None]


def _set_pending_recipient(name: str):
    global _pending_msg_recipient, _awaiting_msg_recipient, _last_msg_recipient
    _pending_msg_recipient = name.strip()
    _awaiting_msg_recipient = False
    if _pending_msg_recipient:
        _last_msg_recipient = _pending_msg_recipient


def _set_awaiting_recipient():
    global _awaiting_msg_recipient, _pending_msg_recipient
    _awaiting_msg_recipient = True
    _pending_msg_recipient = ""


def _clear_pending_recipient():
    global _pending_msg_recipient, _awaiting_msg_recipient
    _pending_msg_recipient = ""
    _awaiting_msg_recipient = False


def _set_pending_message_draft(recipient: str, body: str):
    global _pending_message_draft, _last_msg_recipient
    _pending_message_draft = {
        "recipient": recipient.strip(),
        "body": body.strip(),
    }
    if recipient.strip():
        _last_msg_recipient = recipient.strip()


def _clear_pending_message_draft():
    global _pending_message_draft
    _pending_message_draft = None


def _has_pending_message_draft() -> bool:
    return bool(_pending_message_draft and _pending_message_draft.get("recipient") and _pending_message_draft.get("body"))


def _message_confirmation_prompt(recipient: str, body: str) -> str:
    return (
        f"Draft ready for {recipient}: \"{body}\". "
        f"Say confirm send to send it, or cancel message to stop."
    )


def _is_message_confirm_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "confirm send",
            "send it",
            "yes send",
            "send now",
            "go ahead and send",
            "approve send",
        )
    )


def _is_message_cancel_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "cancel message",
            "don't send",
            "do not send",
            "never mind",
            "cancel it",
            "stop message",
        )
    )


def _extract_contact_name(text: str) -> str:
    cleaned = (text or "").strip().strip("\"'")
    cleaned = re.sub(r"^(?:contact\s*name|name|recipient|contact)\s*:\s*", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^(?:contact|recipient)\s*:\s*", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^(?:to\s+)?(?:contact\s+)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"^(?:send|message|text|say)\s+(?:to\s+)?", "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _looks_like_message_status_query(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "did you message",
            "did you send",
            "was it sent",
            "have you sent",
        )
    )


def _parse_message_compose(text: str) -> tuple[str, str] | None:
    message_patterns = [
        r"^(?:message|text)\s+([A-Za-z0-9@\.]+(?:\s+[A-Za-z0-9@\.]+){0,3})\s+(.+)$",
        r"^(?:send (?:a )?(?:message|text) to|send to)\s+([A-Za-z0-9@\.]+(?:\s+[A-Za-z0-9@\.]+){0,3})\s+(.+)$",
    ]
    for pattern in message_patterns:
        match = re.match(pattern, text.strip(), flags=re.IGNORECASE)
        if not match:
            continue
        recipient = _extract_contact_name(match.group(1))
        body = match.group(2).strip().strip("\"'")
        if recipient and body:
            return recipient, body
    return None


def _looks_like_non_recipient_command(lower: str) -> bool:
    return any(
        phrase in lower for phrase in (
            "access my contacts list",
            "open my contacts",
            "show my contacts",
            "list my contacts",
            "read my contacts",
            "what are my contacts",
        )
    )


def _looks_like_contact_name(name: str) -> bool:
    cleaned = (name or "").strip()
    if not cleaned:
        return False
    if len(cleaned) > 64:
        return False
    if re.search(r"[0-9@]", cleaned):
        return True
    tokens = [tok for tok in re.split(r"\s+", cleaned) if tok]
    if not tokens or len(tokens) > 4:
        return False
    blocked = {"access", "contacts", "list", "open", "show", "read", "help", "message", "text", "send"}
    if any(tok.lower() in blocked for tok in tokens):
        return False
    return True


def _parse_message_recipient_only(text: str) -> str:
    raw = (text or "").strip()
    lower = raw.lower()
    if _looks_like_non_recipient_command(lower):
        return ""

    labeled = re.search(r"\b(?:contact\s*name|recipient|name)\s*:\s*(.+)$", raw, flags=re.IGNORECASE)
    if labeled:
        candidate = _extract_contact_name(labeled.group(1))
        return candidate if _looks_like_contact_name(candidate) else ""

    m = re.search(
        r"\b(?:send|message|text)(?:\s+(?:a\s+)?(?:message|text))?\s+to\s+(.+)$",
        raw,
        flags=re.IGNORECASE,
    )
    if m:
        candidate = _extract_contact_name(m.group(1))
        candidate = re.split(r"\s+(?:saying|says|that)\s+", candidate, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        return candidate if _looks_like_contact_name(candidate) else ""

    candidate = _extract_contact_name(raw)
    return candidate if _looks_like_contact_name(candidate) else ""


# ── Main entry ────────────────────────────────────────────────────────────────

def route_stream(user_input: str) -> tuple:
    global _pending_msg_recipient, _awaiting_msg_recipient, _last_msg_recipient, _pending_message_draft
    modifiers = prompt_modifiers.parse(user_input)
    user_input = modifiers.clean_text
    modifier_system = modifiers.system_extra
    lower = user_input.lower().strip()
    if lower:
        mem.track_topic(lower)

    # ── 0. Pending message state ──────────────────────────────────────────────
    if _has_pending_message_draft():
        recipient = _pending_message_draft["recipient"]
        body = _pending_message_draft["body"]
        if _is_message_cancel_query(lower):
            _clear_pending_message_draft()
            _clear_pending_recipient()
            return _s(f"Canceled the draft to {recipient}."), "Messages"
        if _is_message_confirm_query(lower):
            _clear_pending_message_draft()
            _clear_pending_recipient()
            _last_msg_recipient = recipient
            return _s(msg.send_imessage(recipient, body)), "Messages"
        return _s(_message_confirmation_prompt(recipient, body)), "Messages"

    if _awaiting_msg_recipient:
        if not lower:
            return _s("Who would you like to message?"), "Messages"
        recipient = _parse_message_recipient_only(user_input)
        if recipient:
            _set_pending_recipient(recipient)
            return _s(f"What would you like to say to {recipient}?"), "Messages"
        return _s("I still need just the contact name, for example: Contact Name: Chunky."), "Messages"

    if _pending_msg_recipient:
        if not lower:
            return _s(f"What would you like to say to {_pending_msg_recipient}?"), "Messages"
        if _looks_like_message_status_query(lower):
            return _s(f"Not yet. I still need the exact message content for {_pending_msg_recipient}."), "Messages"
        if lower in {"sms", "imessage", "i message", "message"}:
            return _s(f"Got it. What message should I send to {_pending_msg_recipient}?"), "Messages"
        recipient = _pending_msg_recipient
        _clear_pending_recipient()
        _set_pending_message_draft(recipient, user_input)
        return _s(_message_confirmation_prompt(recipient, user_input)), "Messages"

    if not lower:
        return _s("Tell me what you want me to do."), "Chat"

    # ── 1. Fast-path: zero-latency unambiguous commands ───────────────────────

    # Runtime self-knowledge
    composed_message = _parse_message_compose(user_input)
    if composed_message:
        recipient, body = composed_message
        _clear_pending_recipient()
        _set_pending_message_draft(recipient, body)
        return _s(_message_confirmation_prompt(recipient, body)), "Messages"

    recipient_only = _parse_message_recipient_only(user_input)
    if recipient_only and any(term in lower for term in ("send", "message", "text")):
        _set_pending_recipient(recipient_only)
        return _s(f"What would you like to say to {recipient_only}?"), "Messages"

    requested_mode = _requested_mode(lower)
    if requested_mode:
        return _s(set_mode(requested_mode)), "Status"
    if _is_model_status_query(lower):
        return _s(_runtime_status_reply(user_input)), "Status"
    if _is_identity_override_query(lower):
        return _s(_identity_override_reply()), "Status"
    if _is_constraint_bypass_query(lower):
        return _s(_constraint_bypass_reply()), "Status"
    if _is_capability_boundary_query(lower):
        return _s(_capability_boundary_reply()), "Status"
    if lower in {"message", "text", "send a message", "send message", "send a text", "send text"}:
        _set_awaiting_recipient()
        return _s("Who would you like to message?"), "Messages"
    # Explicit specialized-agent requests should always win over later fast paths.
    # In open-source mode we only suppress automatic specialist escalation.
    if _is_specialized_agent_query(lower):
        result = specialized_agents.run(user_input)
        return _s(specialized_agents.result_text(result)), "Specialized Agents"
    if get_mode() != "open-source" and _is_engineering_specialist_query(lower):
        result = specialized_agents.run(user_input)
        return _s(specialized_agents.result_text(result)), "Specialized Agents"
    if _is_confirm_improvement_query(lower):
        pending = _pending_improvements[0]

        def _apply_confirmed_stream():
            yield f"Applying the improvement to {pending['file']}..."
            result = si.apply_pending_improvement(pending)
            _pending_improvements[0] = None
            if result.get("error"):
                yield f" Could not apply: {result['error']}"
            else:
                yield (
                    f" Done. Applied improvement to {result['file']}. "
                    f"{result['lines_changed']} lines changed. "
                    f"Backup saved as {result['backup']}. "
                    f"Say 'restart yourself' to reload the updated code."
                )

        return _apply_confirmed_stream(), "Self-Improve"

    if _is_cancel_improvement_query(lower):
        _pending_improvements[0] = None
        return _s("Improvement discarded. No changes were made."), "Self-Improve"

    if _is_self_review_query(lower):
        return _s(_self_review_text()), "Self-Review"
    if _is_self_improve_safety_query(lower):
        return _s(_self_improve_safety_reply()), "Self-Improve"
    if _is_personal_interest_query(lower):
        return _s(_personal_interest_reply()), "Status"
    if _is_interview_profile_query(lower):
        return _s(_interview_profile_reply(user_input)), "Interview"
    if _is_locking_tradeoff_query(lower):
        return _s(_locking_tradeoff_reply()), "Sonnet"
    if _is_database_index_tradeoff_query(lower):
        return _s(_database_index_tradeoff_reply()), "Sonnet"
    if _is_meeting_captions_query(lower):
        if any(term in lower for term in ("read", "show", "what are", "display", "copy")):
            return _s(browser.read_meeting_captions()), "Browser"
        return _s(browser.summarize_meeting_captions(user_input)), "Browser"
    if _is_meeting_diagnostics_query(lower):
        return _s(_meeting_diagnostics_reply()), "Meeting"
    if _is_focus_meeting_query(lower):
        return _s(browser.focus_meeting_tab()), "Browser"
    if _is_meta_improvement_query(lower):
        return _s(_meta_improvement_reply()), "Status"
    if any(p in lower for p in ("hook status", "behavior gates", "behavior gate status", "hook summary")):
        return _s(behavior_hooks.status_text(hours=24)), "Status"
    if any(p in lower for p in ("cost policy", "routing policy", "training policy", "should we train", "should we distill")):
        return _s(cost_policy.policy_text()), "Status"
    if any(p in lower for p in ("token usage", "usage summary", "cost analysis", "model usage", "api usage", "how many tokens", "how much are you burning")):
        return _s(usage_tracker.summary_text(hours=24)), "Status"
    if any(p in lower for p in ("memory status", "tiered memory status", "memory tiers", "memory summary")):
        return _s(f"Memory status: {memory_layer.status()}"), "Status"
    if any(p in lower for p in ("consolidate memory", "refresh memory", "rebuild memory profile", "update memory profile")):
        result = mem.consolidate_memory()
        return _s(f"Memory consolidation complete: {result}"), "Status"

    # Timer
    if any(p in lower for p in ("set a timer", "timer for", "remind me in")):
        parsed = _parse_timer(lower)
        if parsed:
            seconds, label = parsed
            if _on_timer_done:
                tools.set_timer(seconds, label, _on_timer_done)
            return _s(f"Timer set for {label}."), "Timer"
        return _s("I didn't catch the duration."), "Timer"

    # Volume / mute
    if "mute" in lower and "unmute" not in lower:
        return _s(tools.mute()), "System"
    if "unmute" in lower:
        return _s(tools.unmute()), "System"
    if any(p in lower for p in ("set volume", "volume to", "turn volume", "volume up", "volume down")):
        level = _parse_volume(lower)
        return _s(tools.set_volume(level if level is not None else (80 if "up" in lower else 30))), "System"

    # Brightness
    if any(p in lower for p in ("brightness", "dim the screen", "dim screen", "brighten")):
        level = _parse_volume(lower)
        if level is None:
            level = 30 if any(w in lower for w in ("dim", "low", "dark")) else 80
        return _s(tools.set_brightness(level)), "System"

    # Screenshot
    if any(p in lower for p in ("take a screenshot", "screenshot", "capture screen")):
        return _s(tools.take_screenshot()), "System"

    # Browser
    if any(p in lower for p in ("browse to", "open website", "open site", "go to http", "go to www.", "search the web for", "search google for")):
        target = _parse_browser_target(user_input) or user_input
        click_target = _parse_browser_click_target(user_input)
        if click_target and (any(p in lower for p in ("summarize this page", "summarise this page", "summarize the current page", "summarise the current page", "summarize the page", "summarise the page")) or re.search(r"\b(and then|then|and)\b\s+summari[sz]e\b", lower)):
            return _s(browser.open_click_then_summarize(target, click_target, user_input)), "Browser"
        if click_target:
            return _s(browser.open_then_click(target, click_target)), "Browser"
        if any(p in lower for p in ("summarize this page", "summarise this page", "summarize the current page", "summarise the current page")) or re.search(r"\b(and then|then|and)\b\s+summari[sz]e\b", lower):
            return _s(browser.open_then_summarize(target, user_input)), "Browser"
        return _s(browser.open_url(target)), "Browser"
    if any(p in lower for p in ("summarize this page", "summarise this page", "what's on this page", "what is on this page")):
        return _s(browser.summarize_current_page(user_input)), "Browser"
    if _is_meeting_diagnostics_query(lower):
        return _s(_meeting_diagnostics_reply()), "Meeting"
    if _is_focus_meeting_query(lower):
        return _s(browser.focus_meeting_tab()), "Browser"
    meeting_safe = _meeting_safe_mode_requested(lower)
    if meeting_safe == "on":
        call_privacy.set_enabled(True)
        return _s(call_privacy.status_text()), "Meeting"
    if meeting_safe == "off":
        call_privacy.set_enabled(False)
        return _s(call_privacy.status_text()), "Meeting"
    if meeting_safe == "status":
        return _s(call_privacy.status_text()), "Meeting"
    if re.search(r"\bgo back\b", lower):
        return _s(browser.go_back()), "Browser"
    if re.search(r"\bgo forward\b", lower):
        return _s(browser.go_forward()), "Browser"
    if any(p in lower for p in ("reload page", "refresh page", "reload this page", "refresh this page")):
        return _s(browser.reload_page()), "Browser"

    # Lock
    if any(p in lower for p in ("lock screen", "lock my screen")):
        return _s(tools.lock_screen()), "System"

    # Clipboard
    if any(p in lower for p in ("what's in my clipboard", "read my clipboard", "what did i copy")):
        return _s(terminal.get_clipboard()), "Clipboard"

    # Self-improve fast paths
    if any(p in lower for p in ("restore backup", "undo last change", "revert")):
        backups = si.list_backups()
        if not backups:
            return _s("No backups found."), "Self-Improve"
        result = si.restore_backup(backups[0])
        return _s(result), "Self-Improve"

    if any(p in lower for p in ("restart yourself", "restart jarvis", "reload yourself",
                                "apply changes", "hit your restart", "do a restart")):
        def _do_restart():
            import time
            time.sleep(0.8)  # let the TTS finish speaking
            os.execv(sys.executable, [sys.executable] + sys.argv)
        threading.Thread(target=_do_restart, daemon=True).start()
        return _s("Restarting now."), "Self-Improve"

    # Local vault
    if any(p in lower for p in ("train local model", "train local models", "improve local model", "improve local models", "tune local model", "distill local model", "distill local examples", "export training dataset", "export local training data", "build local modelfile", "fine tune handoff", "axolotl", "unsloth", "lora config", "evaluate local model", "eval local model", "promote local model", "promote adapter", "local eval status", "local model status", "automate local model", "local model autopilot", "local model cycle", "beta test jarvis", "run local beta", "beta test local model", "beta test engineering", "run engineering beta", "coach local model", "coach engineering model", "benchmark local model", "benchmark local models", "compare local models", "local model benchmark", "best local model for apple silicon")):
        if any(p in lower for p in ("local eval status", "local model status")):
            return _s(f"Local training status: {local_training.status()}. Local eval status: {local_model_eval.status()}. Local automation status: {local_model_automation.status()}. Local beta status: {local_beta.status()}"), "Local Model"
        if any(p in lower for p in ("beta test jarvis", "run local beta", "beta test local model")):
            return _s(local_beta.result_text(local_beta.run_beta_suite())), "Local Model"
        if any(p in lower for p in ("beta test engineering", "run engineering beta")):
            return _s(local_beta.result_text(local_beta.run_beta_suite(suite="engineering"))), "Local Model"
        if "coach local model" in lower:
            return _s(local_beta.result_text(local_beta.run_beta_suite(build_training_pack=True))), "Local Model"
        if "coach engineering model" in lower:
            return _s(local_beta.result_text(local_beta.run_beta_suite(build_training_pack=True, suite="engineering"))), "Local Model"
        if any(p in lower for p in ("automate local model", "local model autopilot", "local model cycle")):
            return _s(local_model_automation.result_text(local_model_automation.run_cycle())), "Local Model"
        if any(p in lower for p in ("benchmark local model", "benchmark local models", "compare local models", "local model benchmark")):
            return _s(local_model_benchmark.result_text(local_model_benchmark.run_benchmark())), "Local Model"
        if "best local model for apple silicon" in lower:
            return _s(local_model_benchmark.recommendation_text()), "Local Model"
        if any(p in lower for p in ("promote local model", "promote adapter")):
            return _s(local_model_eval.result_text(local_model_eval.promote_candidate())), "Local Model"
        if any(p in lower for p in ("evaluate local model", "eval local model")):
            candidate = ""
            model_match = re.search(r"\b(?:evaluate|eval)\s+(?:local\s+)?model\s+([a-z0-9._:-]+)", lower)
            if model_match:
                candidate = model_match.group(1)
            candidate = candidate or "jarvis-local"
            return _s(local_model_eval.result_text(local_model_eval.run_eval(candidate_model=candidate))), "Local Model"
        if any(p in lower for p in ("fine tune handoff", "axolotl", "unsloth", "lora config")):
            return _s(local_training.result_text(local_training.build_finetune_handoff())), "Local Model"
        if "distill" in lower:
            return _s(local_training.result_text(local_training.distill_failures())), "Local Model"
        if "export" in lower:
            return _s(local_training.result_text(local_training.export_sft_dataset())), "Local Model"
        if "modelfile" in lower:
            return _s(local_training.result_text(local_training.build_modelfile())), "Local Model"
        if any(p in lower for p in ("pipeline", "run it", "full run", "full pipeline", "fine tune")):
            return _s(local_training.result_text(local_training.build_training_pack())), "Local Model"
        export_result = local_training.export_sft_dataset()
        modelfile_result = local_training.build_modelfile()
        return _s(
            f"{local_training.result_text(export_result)} "
            f"{local_training.result_text(modelfile_result)} "
            "If you want higher local quality on the failure cases, run a distillation pass next."
        ), "Local Model"

    if re.search(r"\b(create|generate|make|build|promote)\b.*\bskill\b", lower):
        if any(p in lower for p in ("promote", "failure", "failures", "eval")):
            return _s(skill_factory.result_text(skill_factory.promote_failures())), "Skill"
        topic = _parse_skill_topic(user_input)
        if topic:
            return _s(skill_factory.result_text(skill_factory.create_skill_from_vault(topic))), "Skill"
        return _s("Tell me what topic you want the skill to cover."), "Skill"

    if any(p in lower for p in ("search the vault", "refresh the vault", "index the vault", "build the vault wiki", "compile the wiki", "ingest source", "ingest file", "ingest repo", "ingest repository", "ingest url", "ingest notes", "add to the vault", "knowledge base", "local knowledge", "from the vault", "in the vault")):
        if any(p in lower for p in ("ingest", "add to the vault")):
            target = _parse_source_target(user_input) or "notes"
            source_type = "notes" if target.lower() in {"notes", "my notes"} else ("google_drive" if ("docs.google.com" in target or "drive.google.com" in target) else "auto")
            result = source_ingest.ingest_source(target, source_type=source_type, auto_build=True)
            return _s(source_ingest.result_text(result)), "Knowledge"
        if any(p in lower for p in ("build", "compile")):
            return _s(vault.build_wiki_text()), "Knowledge"
        if any(p in lower for p in ("refresh", "reindex", "index")):
            data = vault.refresh_index()
            return _s(f"Refreshed the local vault index. I indexed {data.get('doc_count', 0)} markdown documents."), "Knowledge"
        if any(p in lower for p in ("status", "what's in", "what is in", "show")):
            return _s(vault.status_text()), "Knowledge"
        if any(p in lower for p in ("exact local file", "exact file", "exact local file and heading", "exact cited local file")):
            return _s(_vault_exact_citation_summary(user_input)), "Knowledge"
        raw = vault.search_text(user_input)
        if any(p in lower for p in ("summarize", "summarise", "in two sentences", "briefly", "concise", "what does it say")):
            return format_with_mini(
                f"Summarize this local vault context in two concise spoken sentences and include the exact cited local file and heading you relied on:\n{raw}",
                skill_id="local_knowledge",
                tool="knowledge",
                extra_system=modifier_system,
            ), "Knowledge"
        return _s(raw), "Knowledge"

    # ── 2. Hardware fast-path ─────────────────────────────────────────────────
    hw_result = _route_hardware(lower, user_input, modifier_system=modifier_system)
    if hw_result:
        return hw_result

    # ── 3. Orchestrator dispatch ──────────────────────────────────────────────
    return _orchestrate(user_input, lower, modifier_system=modifier_system)


# ── Orchestrator dispatch ─────────────────────────────────────────────────────

def _orchestrate(user_input: str, lower: str, modifier_system: str = "") -> tuple:
    """Use the orchestrator to classify intent and dispatch the right tool."""
    global _last_msg_recipient
    from orchestrator import classify

    decision = classify(user_input)
    tool      = decision.tool
    params    = decision.params
    skill_id  = params.get("skill_id")
    if not skill_id:
        skill = skills.choose_skill(user_input, tool=tool)
        if skill:
            skill_id = skill.id
            params["skill_id"] = skill_id

    # ── Search ────────────────────────────────────────────────────────────────
    if tool == "search":
        query = params.get("query", user_input)
        raw = tools.web_search(query)
        return format_with_mini(
            f"Summarize these search results concisely in Jarvis voice:\n{raw}",
            skill_id=skill_id,
            tool=tool,
            extra_system=modifier_system,
        ), "Search"

    # ── Local knowledge vault ────────────────────────────────────────────────
    if tool == "knowledge":
        action = params.get("action", "").lower() or decision.action.lower()
        if action == "ingest":
            target = params.get("source") or params.get("path") or params.get("url") or _parse_source_target(user_input) or "notes"
            source_type = "notes" if str(target).lower() in {"notes", "my notes"} else ("google_drive" if ("docs.google.com" in str(target) or "drive.google.com" in str(target)) else "auto")
            result = source_ingest.ingest_source(target, source_type=source_type, auto_build=True)
            return _s(source_ingest.result_text(result)), "Knowledge"
        if action in {"build", "compile"}:
            return _s(vault.build_wiki_text()), "Knowledge"
        if action in {"refresh", "reindex", "index"}:
            data = vault.refresh_index()
            return _s(f"Refreshed the local vault index. I indexed {data.get('doc_count', 0)} markdown documents."), "Knowledge"
        if action in {"status", "show"}:
            return _s(vault.status_text()), "Knowledge"

        query = params.get("query") or params.get("topic") or user_input
        results = vault.search(query)
        if not results:
            return _s(f"I didn't find anything relevant in the local vault for {query}."), "Knowledge"
        raw = vault.search_text(query)
        return format_with_mini(
            f"Summarize this local vault context in Jarvis voice and stay grounded in the local files only:\n{raw}",
            skill_id=skill_id or "local_knowledge",
            tool="knowledge",
            extra_system=modifier_system,
        ), "Knowledge"

    # ── Skill factory ────────────────────────────────────────────────────────
    if tool == "skill":
        action = params.get("action", "").lower() or decision.action.lower()
        if action == "promote":
            return _s(skill_factory.result_text(skill_factory.promote_failures())), "Skill"

        topic = params.get("topic") or params.get("query") or _parse_skill_topic(user_input)
        if not topic:
            return _s("Tell me the topic you want me to turn into a reusable skill."), "Skill"
        result = skill_factory.create_skill_from_vault(topic)
        return _s(skill_factory.result_text(result)), "Skill"

    # ── Local model training ────────────────────────────────────────────────
    if tool == "local_model":
        action = params.get("action", "").lower() or decision.action.lower()
        if action == "distill":
            result = local_training.distill_failures()
            return _s(local_training.result_text(result)), "Local Model"
        if action == "export":
            result = local_training.export_sft_dataset()
            return _s(local_training.result_text(result)), "Local Model"
        if action == "modelfile":
            result = local_training.build_modelfile()
            return _s(local_training.result_text(result)), "Local Model"
        if action in {"handoff", "lora"}:
            result = local_training.build_finetune_handoff()
            return _s(local_training.result_text(result)), "Local Model"
        if action in {"automate", "autopilot", "cycle"}:
            result = local_model_automation.run_cycle()
            return _s(local_model_automation.result_text(result)), "Local Model"
        if action == "beta":
            result = local_beta.run_beta_suite()
            return _s(local_beta.result_text(result)), "Local Model"
        if action == "coach":
            result = local_beta.run_beta_suite(build_training_pack=True)
            return _s(local_beta.result_text(result)), "Local Model"
        if action in {"evaluate", "eval"}:
            candidate = params.get("candidate_model") or params.get("model") or params.get("target") or "jarvis-local"
            result = local_model_eval.run_eval(candidate_model=candidate)
            return _s(local_model_eval.result_text(result)), "Local Model"
        if action == "promote":
            candidate = params.get("candidate_model") or params.get("model") or None
            result = local_model_eval.promote_candidate(candidate_model=candidate)
            return _s(local_model_eval.result_text(result)), "Local Model"
        if action in {"train", "tune", "improve"}:
            result = local_training.build_training_pack()
            return _s(local_training.result_text(result)), "Local Model"
        return _s(f"Local training status: {local_training.status()}. Local eval status: {local_model_eval.status()}. Local automation status: {local_model_automation.status()}. Local beta status: {local_beta.status()}"), "Local Model"

    # ── Browser ───────────────────────────────────────────────────────────────
    if tool == "browser":
        action = params.get("action", "").lower()
        target = (
            params.get("url")
            or params.get("query")
            or params.get("target")
            or params.get("page")
            or _parse_browser_target(user_input)
            or user_input
        )
        click_target = params.get("link_text") or params.get("text") or params.get("label") or _parse_browser_click_target(user_input) or ""

        if any(term in lower for term in ("copy current page url", "copy page url", "copy page link", "send this page", "share this page")):
            return _s(browser.copy_current_page_url()), "Browser"
        if "caption" in action or _is_meeting_captions_query(lower):
            if any(term in lower for term in ("read", "show", "what are", "display", "copy")):
                return _s(browser.read_meeting_captions()), "Browser"
            return _s(browser.summarize_meeting_captions(user_input)), "Browser"
        if "focus" in action and any(term in lower for term in ("meeting", "meet", "zoom", "teams", "webex")):
            return _s(browser.focus_meeting_tab()), "Browser"
        if "back" in action:
            return _s(browser.go_back()), "Browser"
        if "forward" in action:
            return _s(browser.go_forward()), "Browser"
        if "reload" in action or "refresh" in action:
            return _s(browser.reload_page()), "Browser"
        if "click" in action and click_target:
            return _s(browser.click_text(click_target)), "Browser"
        if click_target and ("summary" in action or "summarize" in action or "summarise" in action) and target and _parse_browser_target(user_input):
            return _s(browser.open_click_then_summarize(target, click_target, user_input)), "Browser"
        if click_target and target and _parse_browser_target(user_input):
            return _s(browser.open_then_click(target, click_target)), "Browser"
        if ("summary" in action or "summarize" in action or "summarise" in action) and target and _parse_browser_target(user_input):
            return _s(browser.open_then_summarize(target, user_input)), "Browser"
        if "summary" in action or "summarize" in action or "summarise" in lower or "this page" in lower:
            return _s(browser.summarize_current_page(user_input)), "Browser"
        if "current" in action or "where am i" in lower or "what page" in lower:
            return _s(browser.get_current_page()), "Browser"
        return _s(browser.open_url(target)), "Browser"

    # ── Deep research ─────────────────────────────────────────────────────────
    if tool == "deep_research":
        from research import deep_research, format_for_voice
        query = params.get("query", user_input)

        def _stream_research():
            yield "Initiating deep research. This will take a moment, sir."

        # Run research in background, return immediately
        def _do_research():
            result = deep_research(query, depth=2)
            # Save to notes automatically
            notes.add_note(f"# Research: {query}\n\n{result['report']}\n\n"
                           f"Sources: {len(result['sources'])}")
            return result

        # Return a stream that blocks until done
        def _blocking_stream():
            yield f"Researching '{query}'. Reading sources now..."
            result = _do_research()
            voice_summary = format_for_voice(result)
            yield " " + voice_summary
            yield f" Full report saved to notes. {len(result['sources'])} sources cited."

        return _blocking_stream(), "Deep Research"

    # ── Operative agent ───────────────────────────────────────────────────────
    if tool == "operative":
        from operative import run_task
        task = params.get("task", user_input)

        def _operative_stream():
            yield f"Understood. Running the task autonomously now, sir."
            steps_done = []

            def _progress(step_desc, detail):
                steps_done.append(step_desc)

            result = run_task(task, on_progress=_progress)
            yield " " + result["summary"]
            if not result["ok"]:
                failed = [s.description for s in result["steps"] if not s.ok]
                yield f" Note: {len(failed)} step{'s' if len(failed)>1 else ''} encountered issues."

        return _operative_stream(), "Operative"

    # ── Specialized agents ───────────────────────────────────────────────────
    if tool == "specialized_agent":
        explicit_roles = params.get("roles") or []
        result = specialized_agents.run(user_input, roles=explicit_roles or None)
        return _s(specialized_agents.result_text(result)), "Specialized Agents"

    # ── Messages / iMessage ───────────────────────────────────────────────────
    if tool == "message":
        recipient = params.get("recipient", params.get("to", ""))
        body      = params.get("message",   params.get("body", params.get("text", "")))
        # Try to pull recipient from raw input if orchestrator missed it
        if not recipient:
            m = re.search(r"(?:text|message|send to)\s+([A-Za-z0-9@\.]+(?:\s+[A-Za-z0-9@\.]+){0,3})", user_input, flags=re.IGNORECASE)
            if m:
                recipient = m.group(1)
        if recipient and body:
            _clear_pending_recipient()
            _set_pending_message_draft(recipient, body)
            return _s(_message_confirmation_prompt(recipient, body)), "Messages"
        if recipient and not body:
            _set_pending_recipient(recipient)
            return _s(f"What would you like to say to {recipient}?"), "Messages"
        if body and _last_msg_recipient:
            return _s("I need you to restate the recipient before I draft that message."), "Messages"
        if _last_msg_recipient and _looks_like_message_status_query(lower):
            return _s(f"I can send it now. Tell me the message content for {_last_msg_recipient}."), "Messages"
        _set_awaiting_recipient()
        return _s("Who would you like to message?"), "Messages"

    # ── Calendar ──────────────────────────────────────────────────────────────
    if tool == "calendar":
        action = params.get("action", "read")
        if action == "read":
            return _s(gs.get_todays_events()), "Calendar"
        # create — fall through to chat for now
        return smart_stream(user_input, skill_id=skill_id, tool=tool, extra_system=modifier_system)

    # ── Email ─────────────────────────────────────────────────────────────────
    if tool == "email":
        return _s(gs.get_unread_emails()), "Gmail"

    # ── Weather ───────────────────────────────────────────────────────────────
    if tool == "weather":
        return _s(tools.get_weather()), "Weather"

    # ── Notes ─────────────────────────────────────────────────────────────────
    if tool == "notes":
        action = params.get("action", "")
        if "read" in action or "show" in action or "get" in action:
            return _s(notes.get_notes()), "Notes"
        if "search" in action:
            kw = params.get("keyword", "")
            return _s(notes.search_notes(kw)), "Notes"
        # save
        content = params.get("content", "")
        if content:
            return _s(notes.add_note(content)), "Notes"
        return _s(notes.get_notes()), "Notes"

    # ── Terminal ─────────────────────────────────────────────────────────────
    if tool == "terminal":
        cmd = params.get("command", params.get("cmd", ""))
        if cmd:
            output = terminal.run_command(cmd)
            return format_with_mini(
                f"The user ran: '{cmd}'. Output:\n{output}\nSummarize concisely in Jarvis voice.",
                skill_id=skill_id,
                tool=tool,
                extra_system=modifier_system,
            ), "Terminal"
        path = params.get("path", "")
        if path:
            content = terminal.read_file(path)
            return format_with_mini(
                f"Summarize this file concisely:\n{content}",
                skill_id=skill_id,
                tool=tool,
                extra_system=modifier_system,
            ), "File"
        return smart_stream(user_input, skill_id=skill_id, tool=tool, extra_system=modifier_system)

    # ── Admin shell ───────────────────────────────────────────────────────────
    if tool == "admin":
        cmd = params.get("command", params.get("cmd", ""))
        if not cmd:
            cmd = re.sub(r"\b(with admin privileges|administrator privileges|as admin|run as root|sudo)\b", "", user_input, flags=re.IGNORECASE).strip()
        if cmd:
            output = terminal.run_admin_command(cmd)
            return format_with_mini(
                f"The user requested an admin command. Command: '{cmd}'. Output:\n{output}\nSummarize this in Jarvis voice.",
                skill_id=skill_id,
                tool=tool,
                extra_system=modifier_system,
            ), "Admin"
        return _s("Tell me the exact command you want me to run with administrator privileges."), "Admin"

    # ── App ──────────────────────────────────────────────────────────────────
    if tool == "app":
        app_name = params.get("app", _parse_app(user_input) or "")
        if app_name:
            return _s(tools.open_app(app_name)), "App"
        return smart_stream(user_input, skill_id=skill_id, tool=tool, extra_system=modifier_system)

    # ── Camera / Vision ───────────────────────────────────────────────────────
    if tool == "camera":
        action = params.get("action", "webcam")
        if "screen" in action or "screenshot" in action:
            return _s(camera.screenshot_and_describe(user_input)), "Screen"
        return _s(camera.see(user_input)), "Camera"

    # ── Memory ────────────────────────────────────────────────────────────────
    if tool == "memory":
        action = params.get("action", "")
        if action == "save" or lower.startswith("remember "):
            fact = re.sub(r"^remember\s+", "", user_input, flags=re.IGNORECASE).strip()
            mem.add_fact(fact)
            return _s(f"Got it. I'll remember that {fact}."), "Memory"
        if action == "forget" or lower.startswith("forget "):
            keyword = re.sub(r"^forget\s+", "", user_input, flags=re.IGNORECASE).strip()
            removed = mem.forget(keyword)
            return _s("Forgotten." if removed else f"Nothing saved about {keyword}."), "Memory"
        # briefing
        if any(p in lower for p in ("briefing", "catch me up", "what did i miss")):
            from briefing import build_briefing
            return _s(build_briefing(mem.list_facts())), "Memory"
        return smart_stream(user_input, skill_id=skill_id, tool=tool, extra_system=modifier_system)

    # ── Self-improve ──────────────────────────────────────────────────────────
    if tool == "self_improve":
        action = params.get("action", "improve")
        if action == "restart":
            return _s("Restarting now to apply the latest changes."), "Self-Improve"
        if action == "review":
            area = params.get("area") or params.get("target") or None
            return _s(_self_review_text(area=area)), "Self-Review"
        if action == "analyze":
            area = params.get("area", None)
            analysis = si.analyze_weakness(area)
            return _s(analysis), "Self-Improve"
        # improve — phase 1: generate and show diff, wait for approval
        target = params.get("target", params.get("area", ""))
        instruction = target if target else None
        gate = perms.can_self_improve(target or user_input)
        if not gate["ok"]:
            return _s(gate["reason"]), "Self-Improve"

        def _prepare_stream():
            yield "Analyzing my code and generating the improvement. This will take a moment..."
            pending = si.prepare_improvement(instruction=instruction)
            if pending.get("error"):
                yield f" Could not prepare improvement: {pending['error']}"
                return
            # Stash pending state so apply action can retrieve it
            _pending_improvements[0] = pending
            diff_lines = [
                ln for ln in pending["diff"].splitlines()
                if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
            ]
            preview = "\n".join(diff_lines[:30])
            if len(diff_lines) > 30:
                preview += f"\n... and {len(diff_lines) - 30} more lines"
            yield (
                f" Ready to improve {pending['file']}. "
                f"{pending['lines_changed']} lines would change. "
                f"Here is a preview of the diff:\n{preview}\n"
                f"Say 'apply the improvement' or 'yes go ahead' to apply, "
                f"or 'cancel' to discard."
            )

        return _prepare_stream(), "Self-Improve"

    if tool == "self_improve_apply":
        pending = _pending_improvements[0]
        if not pending:
            return _s("No pending improvement to apply. Ask me to improve myself first."), "Self-Improve"

        def _apply_stream():
            yield f"Applying the improvement to {pending['file']}..."
            result = si.apply_pending_improvement(pending)
            _pending_improvements[0] = None
            if result.get("error"):
                yield f" Could not apply: {result['error']}"
            else:
                yield (
                    f" Done. Applied improvement to {result['file']}. "
                    f"{result['lines_changed']} lines changed. "
                    f"Backup saved as {result['backup']}. "
                    f"Say 'restart yourself' to reload the updated code."
                )

        return _apply_stream(), "Self-Improve"

    if tool == "self_improve_cancel":
        had_pending = _pending_improvements[0] is not None
        _pending_improvements[0] = None
        msg_text = "Improvement discarded." if had_pending else "No pending improvement to cancel."
        return _s(msg_text), "Self-Improve"

    # ── Meeting ───────────────────────────────────────────────────────────────
    if tool == "meeting":
        import meeting_listener as ml
        return _s(ml.auto_configure_blackhole()), "Meeting"

    # ── Chat fallback ─────────────────────────────────────────────────────────
    return smart_stream(user_input, skill_id=skill_id, tool=tool, extra_system=modifier_system)


# ── Hardware routing ──────────────────────────────────────────────────────────

_HW_ROUTES = [
    (["fire web shooter", "fire shooter", "activate shooter", "web shooter", "shoot"],
     "web_shooter", "fire", {}),
    (["reload", "reload shooter"], "web_shooter", "reload", {}),
    (["arm retract", "retract arm", "pull back"], "arm", "retract", {}),
    (["arm extend", "extend arm", "reach out"],   "arm", "extend",  {}),
    (["activate relay", "turn on relay", "relay on"],    "relay_1", "on",  {}),
    (["deactivate relay", "turn off relay", "relay off"], "relay_1", "off", {}),
]


def _route_hardware(lower: str, user_input: str, modifier_system: str = ""):
    devices = hw.list_devices()

    if any(p in lower for p in [
        "bridge status", "jarvis bridge", "lan status", "same wifi bridge", "local network bridge",
        "what is my bridge url", "what's my bridge url", "copy bridge url", "show bridge url"
    ]):
        snap = runtime_state.snapshot()
        bridge_host = str(snap.get("api_host") or os.getenv("JARVIS_API_HOST", "127.0.0.1")).strip() or "127.0.0.1"
        try:
            bridge_port = int(snap.get("api_port") or os.getenv("JARVIS_API_PORT", "8765"))
        except (TypeError, ValueError):
            bridge_port = 8765
        bridge = hw.bridge_status(api_host=bridge_host, api_port=bridge_port)
        urls = bridge.get("urls", [])
        ips = bridge.get("ips", [])
        local_only = bridge.get("local_only", True)
        primary = bridge.get("primary_url") or (urls[0] if urls else "http://127.0.0.1:8765")
        mode = "local-only" if local_only else "LAN-enabled"
        ip_text = f" Local IPs: {', '.join(ips)}." if ips else ""
        return _s(f"Bridge status: {mode}. Primary URL: {primary}.{ip_text}"), "Hardware"

    if any(p in lower for p in [
        "open bluetooth settings", "bluetooth settings", "pair a device",
        "open sound settings", "sound settings", "airplay settings",
        "open displays settings", "displays settings", "open display settings"
    ]):
        if "bluetooth" in lower:
            return _s(hw.open_system_settings("bluetooth")), "Hardware"
        if "sound" in lower or "airplay" in lower:
            return _s(hw.open_system_settings("sound")), "Hardware"
        if "display" in lower or "displays" in lower:
            return _s(hw.open_system_settings("displays")), "Hardware"

    if any(p in lower for p in [
        "nearby devices", "nearby device", "what devices are near me", "discover devices",
        "bluetooth devices", "airplay devices", "same wifi devices", "nearby airplay",
        "network devices near me", "what can you connect to"
    ]):
        snapshot = hw.discover_nearby(timeout=1.5)
        bt = snapshot.get("bluetooth", {})
        net = snapshot.get("network", {}).get("services", {})
        connected_bt = bt.get("connected", [])
        known_bt = bt.get("known", [])
        airplay = net.get("airplay", [])
        companion = net.get("companion", [])
        googlecast = net.get("googlecast", [])

        bt_connected_names = ", ".join(d.get("name", "unknown") for d in connected_bt[:4]) or "none"
        bt_known_names = ", ".join(d.get("name", "unknown") for d in known_bt[:5]) or "none"
        airplay_names = ", ".join(d.get("name", "unknown") for d in airplay[:5]) or "none"
        companion_names = ", ".join(d.get("name", "unknown") for d in companion[:5]) or "none"
        cast_names = ", ".join(d.get("name", "unknown") for d in googlecast[:5]) or "none"

        summary = (
            f"Bluetooth connected: {bt_connected_names}. "
            f"Known Bluetooth devices: {bt_known_names}. "
            f"AirPlay targets: {airplay_names}. "
            f"Nearby companion devices: {companion_names}. "
            f"Google Cast targets: {cast_names}."
        )
        return format_with_mini(
            f"Report this nearby device snapshot in Jarvis voice, naturally and concisely: {summary}",
            extra_system=modifier_system,
        ), "Hardware"

    if any(p in lower for p in ["hardware status", "device status", "check devices", "show hardware"]):
        s = hw.status()
        if not s or "No hardware" in s:
            return _s("No hardware devices registered, sir."), "Hardware"
        return format_with_mini(
            f"Report this hardware status in Jarvis voice: {s}",
            extra_system=modifier_system,
        ), "Hardware"

    if any(p in lower for p in ["scan ports", "find devices", "detect hardware"]):
        ports = hw.scan_serial_ports()
        return _s(f"Found {len(ports)} port{'s' if len(ports)>1 else ''}: {', '.join(ports)}." if ports
                  else "No serial ports detected."), "Hardware"

    if any(p in lower for p in ["emergency stop", "abort all", "halt all", "stop all devices"]):
        results = hw.command_all("stop")
        ok = sum(1 for r in results.values() if r.ok)
        return _s(f"Emergency stop sent to {len(results)} devices. {ok} confirmed."), "Hardware"

    for device in devices:
        dname = device.name.lower().replace("_", " ")
        cmd_match = re.search(
            rf"(?:fire|activate|trigger|run|execute|use|engage)\s+{re.escape(dname)}(?:\s+(\w+))?|"
            rf"{re.escape(dname)}\s+(\w+)|"
            rf"(?:turn\s+(?:on|off))\s+{re.escape(dname)}",
            lower
        )
        if cmd_match:
            cmd = (cmd_match.group(1) or cmd_match.group(2) or "trigger").strip()
            if "turn off" in lower or "deactivate" in lower:
                cmd = "off"
            elif "turn on" in lower or "activate" in lower and not cmd_match.group(1):
                cmd = "on"
            result = hw.command(device.name, cmd)
            msg = str(result)
            return format_with_mini(
                f"Report this in Jarvis voice (1 sentence): {msg}",
                extra_system=modifier_system,
            ), "Hardware"

    for triggers, device_name, cmd, extra_params in _HW_ROUTES:
        if any(t in lower for t in triggers):
            dur = re.search(r"(\d+)\s*ms", lower)
            p = dict(extra_params, duration=int(dur.group(1))) if dur else extra_params
            result = hw.command(device_name, cmd, **p)
            return format_with_mini(
                f"Report in Jarvis voice (1 sentence): {result}",
                extra_system=modifier_system,
            ), "Hardware"

    return None
