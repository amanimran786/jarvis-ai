"""
Scoped prompt modifiers for Jarvis.

These are request-local controls like:
  ELI5: explain TCP
  /BRIEFLY summarize this
  TONE formal: rewrite this note
  ROLE: security reviewer TASK: review this auth flow FORMAT: JSON

They should stay out of the global system prompt and only affect the current
request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


SIMPLE_MODIFIERS = {
    "ELI5": "Explain in very simple plain language. Avoid jargon unless you define it immediately.",
    "TLDL": "Summarize the essential answer in a few short sentences.",
    "STEP-BY-STEP": "Explain the process clearly in sequence. Use natural spoken transitions instead of markdown lists.",
    "CHECKLIST": "Structure the answer like a practical checklist, but keep it voice-safe and plain text.",
    "EXEC SUMMARY": "Lead with a concise executive summary before any supporting detail.",
    "BRIEFLY": "Keep the answer very short and direct.",
    "TERSE": "Use short sentences. Cut filler. Keep the answer compressed, direct, and concrete.",
    "CAVEMAN LITE": "Write in compressed plain English. Keep sentences short, but still readable and polished enough for teammates.",
    "CAVEMAN FULL": "Write in a telegram-like style. Very short sentences. No filler. Keep only the useful information.",
    "CAVEMAN ULTRA": "Use maximum compression. Fragments are acceptable if they stay understandable and accurate.",
    "JARGON": "Use precise technical vocabulary where it improves accuracy.",
    "DEV MODE": "Answer in a direct, technical, implementation-focused developer style.",
    "PM MODE": "Answer from a product-management perspective: tradeoffs, scope, dependencies, and outcome clarity.",
    "SWOT": "Frame the answer as strengths, weaknesses, opportunities, and threats, but keep it plain text and voice-safe.",
    "COMPARE": "Compare the relevant options side by side and highlight the practical difference.",
    "MULTI-PERSPECTIVE": "Show the main competing perspectives before concluding.",
    "REFLECTIVE MODE": "Critically inspect the answer before finalizing it and correct weak reasoning.",
    "SYSTEMATIC BIAS CHECK": "Check the answer for missing perspectives, bias, or unbalanced assumptions.",
    "DELIBERATE THINKING": "Use slower, more careful reasoning and avoid jumping to the first plausible answer.",
    "NO AUTOPILOT": "Do not give a generic or autopilot answer. Stay concrete and specific.",
    "EVAL-SELF": "At the end, briefly critique the answer's main weakness or uncertainty.",
    "PARALLEL LENSES": "Analyze the question through several useful lenses in parallel before concluding.",
    "FIRST PRINCIPLES": "Rebuild the answer from fundamental assumptions instead of surface analogy.",
    "PITFALLS": "Call out likely traps, mistakes, or failure modes.",
    "METRICS MODE": "Express the answer with measurable indicators, thresholds, or observable signals where possible.",
}

PARAMETERIZED_MODIFIERS = {
    "ACT AS": "For this request, adopt this role or persona: {value}.",
    "AUDIENCE": "Tailor the answer to this audience: {value}.",
    "TONE": "Use this tone while staying accurate and concise: {value}.",
    "FORMAT AS": "Produce the answer in this format if possible while staying plain text and voice-safe unless explicitly structured text is needed: {value}.",
    "REWRITE AS": "Rewrite the answer in this style: {value}.",
    "BEGIN WITH": "Begin the answer with this exact phrase or opening: {value}.",
    "END WITH": "End the answer with this exact phrase or closing: {value}.",
    "GUARDRAIL": "Do not cross this boundary for the current request: {value}.",
}

_PARAMETERIZED_NAMES = sorted(PARAMETERIZED_MODIFIERS, key=len, reverse=True)
_SIMPLE_NAMES = sorted(SIMPLE_MODIFIERS, key=len, reverse=True)


@dataclass
class ModifierResult:
    clean_text: str
    system_extra: str = ""
    applied: list[str] = field(default_factory=list)


def _strip_command_prefix(text: str) -> str:
    return text[1:] if text.startswith("/") else text


def _parse_role_task_format(text: str) -> ModifierResult | None:
    stripped = text.strip()
    if not stripped.upper().startswith("ROLE:"):
        return None

    upper = stripped.upper()
    task_idx = upper.find(" TASK:")
    format_idx = upper.find(" FORMAT:")
    if task_idx == -1 or format_idx == -1 or task_idx > format_idx:
        return None

    role_value = stripped[len("ROLE:"):task_idx].strip()
    task_value = stripped[task_idx + len(" TASK:"):format_idx].strip()
    format_value = stripped[format_idx + len(" FORMAT:"):].strip()
    if not role_value or not task_value or not format_value:
        return None

    system_extra = (
        f"For this request, act in the role of {role_value}. "
        f"Present the answer in this format or structure when possible: {format_value}. "
        "Stay plain text and voice-safe unless the user explicitly needs machine-readable output."
    )
    return ModifierResult(
        clean_text=task_value,
        system_extra=system_extra,
        applied=["ROLE:TASK:FORMAT"],
    )


def _parse_parameterized(text: str) -> ModifierResult | None:
    stripped = text.strip()
    upper = _strip_command_prefix(stripped).upper()

    for name in _PARAMETERIZED_NAMES:
        if stripped.startswith("/"):
            prefix = f"/{name}"
            if upper.startswith(name):
                pass
        else:
            prefix = name

        if not upper.startswith(name):
            continue

        body = _strip_command_prefix(stripped)[len(name):].lstrip()
        if not body:
            continue

        if ":" not in body:
            continue

        value, remainder = body.split(":", 1)
        value = value.strip()
        remainder = remainder.strip()
        if not value or not remainder:
            continue

        return ModifierResult(
            clean_text=remainder,
            system_extra=PARAMETERIZED_MODIFIERS[name].format(value=value),
            applied=[name],
        )

    return None


def _parse_simple_prefix(text: str) -> tuple[str | None, str] | None:
    stripped = text.lstrip()
    if not stripped:
        return None

    if stripped.startswith("/"):
        raw = stripped[1:]
        for name in _SIMPLE_NAMES:
            if raw.upper().startswith(name):
                remainder = raw[len(name):].lstrip(" :")
                return name, remainder
        return None

    for name in _SIMPLE_NAMES:
        prefix = f"{name}:"
        if stripped.upper().startswith(prefix):
            remainder = stripped[len(prefix):].lstrip()
            return name, remainder
    return None


def parse(text: str) -> ModifierResult:
    if not text or not text.strip():
        return ModifierResult(clean_text=text or "")

    role_block = _parse_role_task_format(text)
    if role_block:
        return role_block

    applied: list[str] = []
    extras: list[str] = []
    current = text.strip()

    while True:
        parameterized = _parse_parameterized(current)
        if parameterized:
            applied.extend(parameterized.applied)
            if parameterized.system_extra:
                extras.append(parameterized.system_extra)
            current = parameterized.clean_text.strip()
            continue

        simple = _parse_simple_prefix(current)
        if not simple:
            break
        name, remainder = simple
        applied.append(name)
        extras.append(SIMPLE_MODIFIERS[name])
        current = remainder.strip()

    system_extra = "\n".join(extras).strip()
    clean = current.strip()
    if not clean and not applied:
        clean = text.strip()
    return ModifierResult(clean_text=clean, system_extra=system_extra, applied=applied)
