"""
agents/career_agent.py — Career intelligence agent.

Handles: resume tailoring, STAR story extraction, job-match scoring,
and application preparation. All vault writes require explicit approval.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("jarvis.agent.career")

try:
    from infra.memory import store, search
except ImportError:
    store = None
    search = None
    logger.warning("infra.memory unavailable; vector memory disabled.")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _extract_bullets(text: str) -> list[str]:
    """Extract top-level bullets from LLM markdown output."""
    bullets = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ", "• ")):
            bullets.append(stripped[2:].strip())
    return bullets


def _score_match(jd_text: str, resume_keywords: list[str]) -> float:
    """Return 0–1 match score based on keyword overlap."""
    jd_lower = jd_text.lower()
    hits = sum(1 for kw in resume_keywords if kw.lower() in jd_lower)
    return round(hits / max(len(resume_keywords), 1), 2)


# ── Task dispatcher ───────────────────────────────────────────────────────────

def process_task(task: dict[str, Any]) -> dict[str, Any]:
    """
    Entry point for the career agent.

    Supported task types (task["type"]):
      - "resume_tailor"   : rewrite resume bullets for a job description
      - "star_match"      : match STAR stories to job requirements
      - "job_score"       : score job description fit against stored profile
      - "apply_prep"      : produce a cover letter + tailored bullet list

    Required context keys vary by type — see each handler below.
    """
    task_type = task.get("type", "")
    ctx = task.get("context", {})

    handlers = {
        "resume_tailor": _handle_resume_tailor,
        "star_match": _handle_star_match,
        "job_score": _handle_job_score,
        "apply_prep": _handle_apply_prep,
    }

    handler = handlers.get(task_type)
    if handler is None:
        return {
            "ok": False,
            "error": f"Unknown career task type: {task_type!r}",
            "supported_types": list(handlers),
        }

    try:
        result = handler(ctx)
        if store and task.get("save_to_memory"):
            store(
                content=str(result),
                metadata={"agent": "career_agent", "task_type": task_type},
            )
        return {"ok": True, "result": result}
    except Exception as exc:
        logger.exception("career_agent failed on task type %s", task_type)
        return {"ok": False, "error": str(exc)}


# ── Handlers ──────────────────────────────────────────────────────────────────

def _handle_resume_tailor(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Rewrite resume experience bullets to target a specific job description.

    ctx keys:
      - job_description (str): full JD text
      - resume_bullets  (list[str]): existing bullet points
      - role_title      (str, optional): target role title
    """
    jd = ctx.get("job_description", "")
    bullets = ctx.get("resume_bullets", [])
    role = ctx.get("role_title", "the role")

    if not jd:
        return {"error": "job_description is required"}
    if not bullets:
        return {"error": "resume_bullets is required"}

    # Build a structured prompt for the local LLM
    bullet_block = "\n".join(f"- {b}" for b in bullets)
    prompt = (
        f"You are a career coach. Rewrite the following resume bullets to better match "
        f"the job description for {role}. Keep each bullet concise (≤ 20 words), "
        f"action-verb-first, and quantified where possible.\n\n"
        f"JOB DESCRIPTION:\n{jd[:2000]}\n\n"
        f"CURRENT BULLETS:\n{bullet_block}\n\n"
        f"OUTPUT: Rewritten bullets as a numbered list."
    )

    tailored = _local_llm(prompt)
    return {
        "role": role,
        "original_bullets": bullets,
        "tailored_bullets": _extract_bullets(tailored) or tailored,
        "prompt_used": prompt[:300],
    }


def _handle_star_match(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Match stored STAR stories to job requirements.

    ctx keys:
      - job_description  (str): full JD text
      - star_stories     (list[dict]): each has keys: situation, task, action, result
    """
    jd = ctx.get("job_description", "")
    stories = ctx.get("star_stories", [])

    if not jd or not stories:
        return {"error": "job_description and star_stories are required"}

    scored = []
    for story in stories:
        story_text = " ".join([
            story.get("situation", ""),
            story.get("task", ""),
            story.get("action", ""),
            story.get("result", ""),
        ])
        # Extract keywords from the story
        words = re.findall(r"\b[a-zA-Z]{4,}\b", story_text.lower())
        unique_kws = list(set(words))
        score = _score_match(jd, unique_kws)
        scored.append({"story": story, "match_score": score})

    scored.sort(key=lambda x: x["match_score"], reverse=True)
    return {"matches": scored[:5]}


def _handle_job_score(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Score how well a job description matches the user's profile.

    ctx keys:
      - job_description (str): full JD text
      - profile_keywords (list[str], optional): override stored profile
    """
    jd = ctx.get("job_description", "")
    if not jd:
        return {"error": "job_description is required"}

    keywords = ctx.get("profile_keywords") or _recall_profile_keywords()
    if not keywords:
        return {"error": "No profile keywords found. Provide profile_keywords in context."}

    score = _score_match(jd, keywords)
    matched = [kw for kw in keywords if kw.lower() in jd.lower()]
    missing = [kw for kw in keywords if kw.lower() not in jd.lower()]

    return {
        "match_score": score,
        "matched_keywords": matched,
        "missing_keywords": missing[:10],
        "recommendation": "Strong match" if score >= 0.6 else "Partial match" if score >= 0.3 else "Weak match",
    }


def _handle_apply_prep(ctx: dict[str, Any]) -> dict[str, Any]:
    """
    Generate a cover letter draft and tailored bullet list for a job.

    ctx keys:
      - job_description (str): full JD text
      - company         (str): company name
      - role_title      (str): position being applied to
      - resume_summary  (str, optional): 2-3 sentence personal summary
    """
    jd = ctx.get("job_description", "")
    company = ctx.get("company", "the company")
    role = ctx.get("role_title", "the role")
    summary = ctx.get("resume_summary", "")

    if not jd:
        return {"error": "job_description is required"}

    prompt = (
        f"Write a concise, non-generic cover letter paragraph (3-4 sentences) for a {role} position "
        f"at {company}. Use this personal summary: {summary or 'Not provided'}. "
        f"Draw from this job description:\n{jd[:1500]}\n\n"
        f"Then provide 3 specific bullet points showing relevant accomplishments."
    )

    draft = _local_llm(prompt)
    return {
        "company": company,
        "role": role,
        "cover_letter_draft": draft,
        "note": "Review and personalize before sending. vault_propose() before saving.",
    }


# ── LLM integration ───────────────────────────────────────────────────────────

def _local_llm(prompt: str) -> str:
    """Call local Ollama brain. Falls back to structured placeholder on failure."""
    try:
        from brains.brain_ollama import ask_local
        return ask_local(prompt)
    except Exception as exc:
        logger.warning("Local LLM unavailable: %s", exc)
        return f"[LLM unavailable — prompt was: {prompt[:100]}...]"


def _recall_profile_keywords() -> list[str]:
    """Try to fetch career profile keywords from vector memory."""
    if search is None:
        return []
    try:
        hits = search("career profile skills keywords", top_k=3)
        text = " ".join(h.get("content", "") for h in hits)
        return list(set(re.findall(r"\b[a-zA-Z]{4,}\b", text.lower())))[:40]
    except Exception:
        return []
