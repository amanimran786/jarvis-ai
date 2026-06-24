"""
local_runtime/prompt_optimizer.py

Autonomous prompt engineering loop.

Given a PromptTemplate and a list of EvalCases, iterates until output
quality meets a score threshold and all grounding checks pass:

  1. Render the prompt, run each case through a local (or cloud) model
  2. Score each output with an LLM judge (0–5 scale → normalised 0–1)
  3. Grounding-check outputs against provided reference data (anti-hallucination)
  4. If below threshold: identify the failing component, revise it, loop
  5. When threshold met or max_iterations reached: return OptimizationResult

Local-first: allow_cloud=False by default, uses Ollama via ask_local.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_JUDGE_TEACHER_CLOUD = "claude-haiku-4-5-20251001"
_MAX_GROUNDING_INPUT = 2000
_MAX_OUTPUT_SAMPLE = 1000
_MAX_FAILURES_TEXT = 1500

_VALID_COMPONENTS = frozenset(
    {"role_context", "instructions", "output_format", "examples", "tone_context"}
)


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------

@dataclass
class PromptTemplate:
    """Structured prompt artifact following Anthropic's 5-component pattern.

    render() substitutes {{VARIABLE}} placeholders and assembles the
    components into a single system-prompt string.
    """
    role_context: str           # Component 1: role + high-level task
    instructions: str           # Component 3: detailed task instructions
    output_format: str          # Component 5/9: output format & constraints
    examples: str = ""          # Component 4: n-shot examples (optional)
    tone_context: str = ""      # Component 2: tone / style guidance
    thinking_instruction: str = ""  # Component 8: "think step by step…"

    def render(self, variables: dict[str, str] | None = None) -> str:
        """Assemble all components in canonical order, skipping empty ones."""
        parts: list[str] = []
        for text in (
            self.role_context,
            self.tone_context,
            self.instructions,
            f"<examples>\n{self.examples}\n</examples>" if self.examples else "",
            self.thinking_instruction,
            self.output_format,
        ):
            stripped = text.strip()
            if stripped:
                parts.append(stripped)
        full = "\n\n".join(parts)
        for key, value in (variables or {}).items():
            full = full.replace(f"{{{{{key}}}}}", value)
        return full

    def copy(self) -> PromptTemplate:
        return PromptTemplate(
            role_context=self.role_context,
            instructions=self.instructions,
            output_format=self.output_format,
            examples=self.examples,
            tone_context=self.tone_context,
            thinking_instruction=self.thinking_instruction,
        )


@dataclass
class EvalCase:
    """One test case: what to ask, what a good answer looks like, and optional grounding."""
    user_message: str
    expected: str                           # Description of a correct response
    input_vars: dict[str, str] = field(default_factory=dict)  # Prompt variables
    grounding: str = ""                     # Reference data for anti-hallucination check
    criteria: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    case: EvalCase
    output: str
    score: float            # 0.0 – 1.0 (normalised from judge's 0–5)
    passed: bool
    grounding_failures: list[str]
    rationale: str


@dataclass
class OptimizationResult:
    prompt: PromptTemplate
    iterations: int
    final_score: float
    case_results: list[CaseResult]
    commit_ready: bool          # True iff score >= threshold AND no grounding failures
    revision_log: list[str]     # One entry per revision: what changed and why


# ---------------------------------------------------------------------------
# Internal: grounding check (anti-hallucination)
# ---------------------------------------------------------------------------

_GROUNDING_CHECK_SYSTEM = (
    "You are a rigorous fact-checker. Given reference data and an AI response, "
    "identify any factual claims in the response that CANNOT be verified from "
    "the reference data. Only flag concrete factual assertions "
    "(numbers, names, specific statements of fact). Ignore opinions and advice."
)

_GROUNDING_CHECK_PROMPT = """\
Reference data:
<reference>
{grounding}
</reference>

AI response to verify:
<response>
{output}
</response>

List each unverifiable factual claim on a separate line.
If every claim is verifiable — or the response makes no factual claims — \
respond exactly: GROUNDED"""


def _check_grounding(
    output: str,
    grounding: str,
    *,
    allow_cloud: bool = False,
) -> list[str]:
    """Return list of ungrounded claims. Empty list = output is grounded.

    Fails open: if the check itself fails (model unavailable), returns []
    so transient infra errors don't block the loop.
    """
    from brains.brain_ollama import ask_local, get_best_available
    from provider_priority import ask_with_priority

    prompt = _GROUNDING_CHECK_PROMPT.format(
        grounding=grounding[:_MAX_GROUNDING_INPUT],
        output=output[:_MAX_OUTPUT_SAMPLE],
    )
    try:
        if allow_cloud:
            result = ask_with_priority(prompt, tier="cheap")
        else:
            model = get_best_available(None)
            result = ask_local(
                prompt, model=model,
                system_extra=_GROUNDING_CHECK_SYSTEM,
                track_context=False, include_memory=False,
            )
        result = result.strip()
        if result.upper().startswith("GROUNDED"):
            return []
        return [line.strip() for line in result.splitlines() if line.strip()]
    except Exception:
        log.debug("[prompt-optimizer] grounding check failed — failing open", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Internal: judge scoring
# ---------------------------------------------------------------------------

_JUDGE_SYSTEM = (
    "You are an expert evaluator. Score AI responses honestly. "
    "Return only valid JSON."
)

_JUDGE_PROMPT = """\
Score this AI response on a scale from 1 (very poor) to 5 (excellent).

Task given to the AI:
<task>{task}</task>

Expected output characteristics:
<expected>{expected}</expected>

Actual response:
<response>{output}</response>

Respond with JSON only:
{{"pass": true/false, "score": 1-5, "rationale": "one sentence"}}"""


def _judge_output(
    task: str,
    expected: str,
    output: str,
    *,
    teacher_model: str,
    allow_cloud: bool = False,
) -> tuple[float, bool, str]:
    """Returns (score_0_to_1, passed, rationale). Scores 4+ → passed."""
    from brains.brain_ollama import ask_local
    from provider_priority import ask_with_priority

    prompt = _JUDGE_PROMPT.format(
        task=task[:500],
        expected=expected[:300],
        output=output[:800],
    )
    try:
        if allow_cloud:
            raw = ask_with_priority(prompt, tier="cheap")
        else:
            raw = ask_local(
                prompt, model=teacher_model,
                system_extra=_JUDGE_SYSTEM,
                track_context=False, include_memory=False,
                raise_on_error=True, strict_model=True,
            )
        # Strip markdown fences if present
        raw = raw.strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.splitlines()[1:-1]).strip()
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if match:
            raw = match.group(0)
        data = json.loads(raw)
        raw_score = float(data.get("score", 0.0))
        score = min(1.0, max(0.0, raw_score / 5.0))
        passed = bool(data.get("pass", False))
        rationale = str(data.get("rationale", ""))
        return score, passed, rationale
    except Exception as e:
        log.warning("[prompt-optimizer] judge call failed: %s", e)
        return 0.0, False, str(e)


# ---------------------------------------------------------------------------
# Internal: single case execution
# ---------------------------------------------------------------------------

def _run_case(
    template: PromptTemplate,
    case: EvalCase,
    *,
    teacher_model: str,
    allow_cloud: bool = False,
) -> CaseResult:
    from brains.brain_ollama import ask_local, get_best_available
    from provider_priority import ask_with_priority

    system = template.render(case.input_vars)

    try:
        if allow_cloud:
            output = ask_with_priority(case.user_message, tier="cheap", system_extra=system)
        else:
            model = get_best_available(None)
            output = ask_local(
                case.user_message, model=model,
                system_extra=system,
                track_context=False, include_memory=False,
            )
    except Exception as e:
        log.warning("[prompt-optimizer] model call failed: %s", e)
        return CaseResult(
            case=case, output="", score=0.0, passed=False,
            grounding_failures=["model unavailable"], rationale=str(e),
        )

    score, passed, rationale = _judge_output(
        case.user_message, case.expected, output,
        teacher_model=teacher_model, allow_cloud=allow_cloud,
    )

    grounding_failures: list[str] = []
    if case.grounding:
        grounding_failures = _check_grounding(output, case.grounding, allow_cloud=allow_cloud)

    return CaseResult(
        case=case,
        output=output,
        score=score,
        passed=passed and not grounding_failures,
        grounding_failures=grounding_failures,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Internal: failure summarisation
# ---------------------------------------------------------------------------

def _summarize_failures(results: list[CaseResult]) -> str:
    lines: list[str] = []
    for i, r in enumerate(results):
        if not r.passed:
            lines.append(f"Case {i + 1} (score {r.score:.2f}):")
            lines.append(f"  Expected: {r.case.expected[:150]}")
            lines.append(f"  Got:      {r.output[:200]}")
            lines.append(f"  Reason:   {r.rationale[:150]}")
            if r.grounding_failures:
                lines.append(f"  Ungrounded: {'; '.join(r.grounding_failures[:3])}")
    return "\n".join(lines) if lines else "No failures."


# ---------------------------------------------------------------------------
# Internal: prompt revision
# ---------------------------------------------------------------------------

_REVISION_SYSTEM = (
    "You are an expert prompt engineer. When a structured AI prompt produces "
    "incorrect or grounding-failed outputs, you revise exactly ONE component "
    "to fix the failures. Be surgical — do not rewrite what is working."
)

_REVISION_PROMPT = """\
A structured prompt was evaluated and failed its quality check. \
Revise exactly ONE component to address the failures below.

Current prompt components:
<role_context>{role_context}</role_context>
<tone_context>{tone_context}</tone_context>
<instructions>{instructions}</instructions>
<output_format>{output_format}</output_format>
<examples>{examples}</examples>

Failures:
<failures>
{failures}
</failures>

Pick the ONE component most responsible for these failures. Respond in this exact format:
<component>role_context</component>  ← or: instructions, output_format, examples, tone_context
<revision>...revised component text (complete replacement)...</revision>
<rationale>...one sentence: what you changed and why...</rationale>"""


@dataclass
class _RevisionResult:
    prompt: PromptTemplate
    component: str
    rationale: str


def _revise_prompt(
    template: PromptTemplate,
    failures: str,
    *,
    allow_cloud: bool = False,
) -> _RevisionResult | None:
    """Ask an LLM to patch the single component most responsible for failures.

    Returns None if the response can't be parsed or the LLM call fails.
    """
    from brains.brain_ollama import ask_local, get_best_available
    from provider_priority import ask_with_priority

    prompt = _REVISION_PROMPT.format(
        role_context=template.role_context,
        tone_context=template.tone_context,
        instructions=template.instructions,
        output_format=template.output_format,
        examples=template.examples[:500],
        failures=failures[:_MAX_FAILURES_TEXT],
    )
    try:
        if allow_cloud:
            raw = ask_with_priority(prompt, tier="strong")
        else:
            model = get_best_available(None)
            raw = ask_local(
                prompt, model=model,
                system_extra=_REVISION_SYSTEM,
                track_context=False, include_memory=False,
                raise_on_error=True, strict_model=True,
            )
    except Exception:
        log.warning("[prompt-optimizer] revision LLM call failed", exc_info=True)
        return None

    component_m = re.search(r"<component>\s*(.*?)\s*</component>", raw, re.DOTALL)
    revision_m = re.search(r"<revision>(.*?)</revision>", raw, re.DOTALL)
    rationale_m = re.search(r"<rationale>(.*?)</rationale>", raw, re.DOTALL)

    if not component_m or not revision_m:
        log.warning("[prompt-optimizer] could not parse revision; raw=%.200s", raw)
        return None

    component = component_m.group(1).strip().lower()
    if component not in _VALID_COMPONENTS:
        log.warning("[prompt-optimizer] unknown component %r in revision", component)
        return None

    revision_text = revision_m.group(1).strip()
    rationale = rationale_m.group(1).strip() if rationale_m else "(no rationale)"

    updated = template.copy()
    setattr(updated, component, revision_text)
    return _RevisionResult(prompt=updated, component=component, rationale=rationale)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def optimize_prompt(
    template: PromptTemplate,
    cases: list[EvalCase],
    *,
    pass_threshold: float = 0.8,
    max_iterations: int = 5,
    teacher_model: str = _JUDGE_TEACHER_CLOUD,
    allow_cloud: bool = False,
) -> OptimizationResult:
    """Iteratively refine *template* until it scores >= *pass_threshold* on
    every *case* and all grounding checks pass, or *max_iterations* is hit.

    commit_ready=True iff:  final_score >= pass_threshold AND no grounding failures.
    """
    if not cases:
        raise ValueError("optimize_prompt requires at least one EvalCase")

    current = template.copy()
    revision_log: list[str] = []
    results: list[CaseResult] = []
    last_score = 0.0
    last_grounding_failures: list[str] = []

    for iteration in range(1, max_iterations + 1):
        log.info("[prompt-optimizer] iteration %d/%d", iteration, max_iterations)

        results = [
            _run_case(current, case, teacher_model=teacher_model, allow_cloud=allow_cloud)
            for case in cases
        ]
        last_score = sum(r.score for r in results) / len(results)
        last_grounding_failures = [f for r in results for f in r.grounding_failures]

        log.info(
            "[prompt-optimizer] score=%.2f  grounding_failures=%d",
            last_score, len(last_grounding_failures),
        )

        if last_score >= pass_threshold and not last_grounding_failures:
            log.info("[prompt-optimizer] threshold met at iteration %d", iteration)
            return OptimizationResult(
                prompt=current,
                iterations=iteration,
                final_score=last_score,
                case_results=results,
                commit_ready=True,
                revision_log=revision_log,
            )

        if iteration < max_iterations:
            failures_text = _summarize_failures(results)
            revision = _revise_prompt(current, failures_text, allow_cloud=allow_cloud)
            if revision is None:
                log.warning("[prompt-optimizer] revision failed; stopping early")
                break
            current = revision.prompt
            revision_log.append(
                f"iter {iteration}: revised <{revision.component}> — {revision.rationale[:120]}"
            )

    return OptimizationResult(
        prompt=current,
        iterations=max_iterations,
        final_score=last_score,
        case_results=results,
        commit_ready=last_score >= pass_threshold and not last_grounding_failures,
        revision_log=revision_log,
    )
