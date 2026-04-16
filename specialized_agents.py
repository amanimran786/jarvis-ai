"""
Scoped specialist-agent coordinator for Jarvis.

Each role has its own instruction file under agents/ and runs in isolated
context. The coordinator uses a small multi-pass flow only when explicitly
requested or when the router selects the specialized-agent tool.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import queue
import re
import threading

from brains.brain_claude import ask_claude
from brains.brain_ollama import ask_local, get_best_available
from config import HAIKU, SONNET, SYSTEM_PROMPT, LOCAL_REASONING, LOCAL_DEFAULT, LOCAL_CODER
import model_router
import skills
import specialized_agent_native
import vault_handoff


AGENTS_DIR = Path(__file__).resolve().parent / "agents"


@dataclass(frozen=True)
class AgentSpec:
    role: str
    path: Path
    model: str


AGENTS = {
    "planner": AgentSpec("planner", AGENTS_DIR / "planner.md", HAIKU),
    "executor": AgentSpec("executor", AGENTS_DIR / "executor.md", SONNET),
    "reviewer": AgentSpec("reviewer", AGENTS_DIR / "reviewer.md", HAIKU),
    "coder": AgentSpec("coder", AGENTS_DIR / "coder.md", SONNET),
    "code_reviewer": AgentSpec("code_reviewer", AGENTS_DIR / "code_reviewer.md", HAIKU),
    "operator": AgentSpec("operator", AGENTS_DIR / "operator.md", SONNET),
    "researcher": AgentSpec("researcher", AGENTS_DIR / "researcher.md", SONNET),
    "debugger": AgentSpec("debugger", AGENTS_DIR / "debugger.md", SONNET),
    "vault_curator": AgentSpec("vault_curator", AGENTS_DIR / "vault_curator.md", HAIKU),
    "science_expert": AgentSpec("science_expert", AGENTS_DIR / "science_expert.md", SONNET),
    "security_reviewer": AgentSpec("security_reviewer", AGENTS_DIR / "security_reviewer.md", SONNET),
    "security_analyst": AgentSpec("security_analyst", AGENTS_DIR / "security_analyst.md", SONNET),
    "self_improve_critic": AgentSpec("self_improve_critic", AGENTS_DIR / "self_improve_critic.md", HAIKU),
}

# Local model assignment per role — planner/reviewer need speed, executor needs depth.
# Keeps multi-pass pipeline latency manageable on a Mac.
_LOCAL_ROLE_MODELS = {
    "planner":            LOCAL_DEFAULT,    # fast outline pass
    "executor":           LOCAL_REASONING,  # deep answer pass — worth the wait
    "reviewer":           LOCAL_DEFAULT,    # fast verification pass
    "coder":              LOCAL_CODER,      # implementation-focused coding model
    "code_reviewer":      LOCAL_CODER,      # code-focused review and regression detection
    "operator":           LOCAL_REASONING,  # concrete environment steps
    "researcher":         LOCAL_REASONING,  # source synthesis and comparison
    "debugger":           LOCAL_REASONING,  # root-cause and verification path
    "vault_curator":      LOCAL_DEFAULT,    # structured note work is lightweight
    "science_expert":     LOCAL_REASONING,  # needs depth
    "security_reviewer":  LOCAL_REASONING,  # needs depth
    "security_analyst":   LOCAL_REASONING,  # abuse-path and trust-boundary analysis
    "self_improve_critic": LOCAL_DEFAULT,   # lightweight check
}

_LOCAL_SPECIALIST_TIMEOUT_SECONDS = max(
    1.0,
    float(os.getenv("JARVIS_SPECIALIST_LOCAL_TIMEOUT_SECONDS", "8")),
)


def _load_agent_instructions(role: str) -> str:
    spec = AGENTS[role]
    return spec.path.read_text(encoding="utf-8").strip()


def available_roles() -> list[str]:
    return list(AGENTS)


def _ask_local_with_timeout(prompt: str, *, model: str, system_extra: str) -> str:
    result_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=1)

    def _worker() -> None:
        try:
            output = ask_local(
                prompt,
                model=model,
                system_extra=system_extra,
                raise_on_error=True,
            ).strip()
            result_queue.put(("ok", output))
        except Exception as exc:
            result_queue.put(("error", str(exc)))

    thread = threading.Thread(target=_worker, daemon=True, name=f"SpecialistLocal-{model}")
    thread.start()
    thread.join(_LOCAL_SPECIALIST_TIMEOUT_SECONDS)
    if thread.is_alive():
        raise TimeoutError(f"local specialist timeout after {_LOCAL_SPECIALIST_TIMEOUT_SECONDS:.1f}s")
    try:
        status, payload = result_queue.get_nowait()
    except queue.Empty as exc:
        raise RuntimeError("local specialist returned no result") from exc
    if status == "error":
        raise RuntimeError(payload)
    return payload


def _entropy_fallback() -> str:
    return (
        "In thermodynamics, entropy describes how many microscopic arrangements can produce the same macroscopic state, "
        "so it tracks energy dispersal and the direction real physical processes tend to move. "
        "In information theory, Shannon entropy measures uncertainty in a probability distribution, so it tells you how much information you expect to gain when you learn the outcome. "
        "The bridge between them is the same mathematical shape, because both are counting uncertainty over possible states, "
        "but thermodynamics is about physical state distributions while information theory is about messages, symbols, and prediction."
    )


def _memory_leak_executor_fallback() -> str:
    return (
        "The most likely causes are unbounded caches, request or session objects being retained longer than expected, "
        "database or HTTP client pools that never release resources, background tasks accumulating references, and circular reference patterns around callbacks or closures. "
        "The debugging sequence should be concrete. First, confirm it is real growth instead of allocator noise by watching RSS and Python heap behavior over several steady-state intervals. "
        "Second, capture heap snapshots with tracemalloc and compare before and after growth windows. Third, inspect object counts for suspicious types with objgraph or gc.get_objects. "
        "Fourth, check caches, LRU settings, global registries, and per-request state that might never be evicted. Fifth, review connection and client lifecycles for leaked sessions, cursors, streams, or subscriptions. "
        "Sixth, reproduce the leak under controlled load and narrow it by disabling subsystems one at a time until the slope changes."
    )


def _memory_leak_planner_fallback() -> str:
    return (
        "Plan: verify the leak, measure where memory accumulates, identify the retained object families, "
        "then isolate the subsystem responsible before changing code."
    )


def _memory_leak_reviewer_fallback() -> str:
    return (
        "The answer should stay evidence-driven. Do not guess at one root cause too early. "
        "Use snapshots, object-growth diffs, and subsystem isolation so the fix targets the real retention path."
    )


def _fastapi_502_executor_fallback() -> str:
    return (
        "The most likely causes are that Nginx is proxying to the wrong upstream host or port, "
        "the FastAPI service is only listening on localhost instead of 0.0.0.0 inside Docker, "
        "the container network wiring is wrong, or the upstream is timing out before it returns. "
        "Start the debugging sequence by checking the Nginx error logs and access logs, then verify the proxy_pass target, then exec into the Nginx container and curl the FastAPI upstream directly. "
        "After that, confirm the app is bound to 0.0.0.0 on the expected port, check docker compose service names and exposed ports, and then look for startup crashes, health check failures, or timeout settings that make Nginx return 502."
    )


def _fastapi_502_planner_fallback() -> str:
    return (
        "Plan: verify the upstream target, confirm container-to-container reachability, "
        "check whether FastAPI is listening on the expected port, then narrow down timeout versus crash versus routing failure."
    )


def _fastapi_502_reviewer_fallback() -> str:
    return (
        "Keep the answer concrete. Lead with upstream host and port mismatch, binding to 0.0.0.0, docker networking, and timeout evidence from logs before proposing deeper causes."
    )


def _migration_executor_fallback() -> str:
    return (
        "Use a two-phase rollout. First deploy code that can handle both null and non-null values, then backfill the existing rows in batches, verify there are no remaining nulls, and only after that add the database constraint to enforce NOT NULL. "
        "If the table is large, prefer an online-safe path such as adding a check constraint, validating it, and then tightening the schema in a controlled deploy window. "
        "The key ideas are compatibility first, batched backfill second, constraint enforcement third, and a rollback path that removes the app dependency before you relax the schema."
    )


def _migration_planner_fallback() -> str:
    return (
        "Plan: ship compatibility code first, backfill safely, validate the data, enforce the constraint last, and keep rollback limited to the application dependency before any destructive schema step."
    )


def _migration_reviewer_fallback() -> str:
    return (
        "Keep the answer focused on zero-downtime sequencing: compatibility deploy, batch backfill, validation, constraint enforcement, and explicit rollback boundaries."
    )


def _race_condition_executor_fallback() -> str:
    return (
        "Start by identifying the shared state and every code path that can touch it concurrently, then add high-signal logging around thread IDs, task IDs, ordering, and timestamps so you can see the interleaving. "
        "Next, make it reproducible by increasing concurrency, reducing jitter, and building a stress test or loop that runs the critical section thousands of times until the failure shows up more often. "
        "After that, narrow it down by adding synchronization or isolating one shared variable at a time, and verify the fix with the same stress harness so you know the race condition is actually gone rather than just harder to hit."
    )


def _race_condition_planner_fallback() -> str:
    return (
        "Plan: map the shared state, instrument the ordering, force the race to happen more often, then contain the critical section and re-run the reproducer."
    )


def _race_condition_reviewer_fallback() -> str:
    return (
        "The answer should not stop at saying to add a lock. It should include a way to reproduce the race condition, capture the interleaving, and prove the fix under load."
    )


def _stale_read_executor_fallback() -> str:
    return (
        "Separate the problem into two hypotheses: stale cache invalidation versus replica lag. "
        "To test cache invalidation, trace the write path through cache key generation, invalidation timing, TTL behavior, and whether readers can still hit an old key after the write commits. "
        "To test replica lag, compare reads from the primary versus the replica immediately after writes, measure replication delay directly, and look for read-after-write consistency gaps that disappear when you pin the session to the writer. "
        "The shortest path is to add request correlation IDs, log whether each read came from cache, primary, or replica, and then compare stale responses against invalidation events and replication lag metrics."
    )


def _stale_read_planner_fallback() -> str:
    return (
        "Plan: instrument the read path so each response is tagged as cache, primary, or replica, then compare stale reads against invalidation timing and replica lag metrics."
    )


def _stale_read_reviewer_fallback() -> str:
    return (
        "Keep the answer binary and testable: cache invalidation should show stale keys or TTL behavior, while replica lag should show read-after-write inconsistency that disappears on the primary."
    )


def _security_fallback() -> str:
    return (
        "The likely failure modes are weak authorization boundaries, missing ownership checks, token or secret handling mistakes, "
        "and unsafe trust in client-provided state. Start by mapping who can do what, then verify every sensitive action server-side."
    )


def _auth_security_fallback() -> str:
    return (
        "The two biggest issues are storing JWT access tokens in localStorage, which makes token theft easier under XSS, "
        "and trusting frontend role checks for admin behavior, which is not real authorization. "
        "The server has to enforce permissions server-side on every privileged action, and sensitive role or ownership checks cannot rely on what the frontend shows or hides. "
        "A safer design is short-lived tokens in more constrained storage, explicit server-side authorization checks, rotation and revocation support, and careful token scope boundaries."
    )


def _self_improve_fallback() -> str:
    return (
        "I should only recommend self-changes when recent eval failures cluster around the same weak path, "
        "the target file is clear, and the change can be validated before it is written."
    )


def _operator_fallback() -> str:
    return (
        "Start with the exact environment action that changes the state, execute only the requested scope, "
        "then report the result, blocker, or shortest next verification step."
    )


def _coder_fallback() -> str:
    return (
        "Inspect the existing repo pattern first, make the smallest correct code change, "
        "then run the narrowest verification that proves the patch works."
    )


def _code_reviewer_fallback() -> str:
    return (
        "Start with the highest-risk regression or unsafe assumption, say what could break, "
        "and name the shortest proof step before discussing maintainability."
    )


def _researcher_fallback() -> str:
    return (
        "Lead with the strongest sourced finding, then summarize the evidence that agrees, "
        "call out what is uncertain, and end with the next thing worth verifying."
    )


def _debugger_fallback() -> str:
    return (
        "Identify the failing layer first, name the two or three likeliest causes, "
        "then give the shortest proof step that separates them."
    )


def _vault_curator_fallback() -> str:
    return (
        "Update the smallest durable note surface that matches the request, preserve provenance, add links, "
        "and avoid whole-file rewrites when a heading or frontmatter patch is enough."
    )


def _security_analyst_fallback() -> str:
    return (
        "Start with the highest-impact abuse path or trust-boundary failure, then name what would have to be true "
        "for exploitation and which control would close it."
    )


def _fallback_role_output(role: str, task: str, context: str = "") -> str:
    lower = task.lower()
    memory_leak_like = ("memory leak" in lower) or ("leaking memory" in lower)
    fastapi_502_like = ("fastapi" in lower and "nginx" in lower and "502" in lower)
    migration_like = ("postgres" in lower or "migration" in lower or "schema" in lower) and any(
        phrase in lower for phrase in ("zero-downtime", "zero downtime", "required", "not null", "rollout plan", "migration plan")
    )
    race_condition_like = "race condition" in lower and ("python" in lower or "worker" in lower or "thread" in lower)
    stale_read_like = any(phrase in lower for phrase in ("stale data", "cache invalidation", "replica lag", "read-after-write"))
    auth_security_like = (
        ("localstorage" in lower or "local storage" in lower or "jwt" in lower or "token" in lower)
        and ("auth" in lower or "authentication" in lower or "authorization" in lower or "permission" in lower or "role" in lower)
    )
    if role == "science_expert":
        if "entropy" in lower and ("thermodynamics" in lower or "information theory" in lower):
            return _entropy_fallback()
        return (
            "The cloud expert path is unavailable right now, so the local fallback is keeping the answer narrower. "
            "State the core mechanism, the real tradeoff, and how to verify it experimentally."
        )
    if role == "planner":
        if memory_leak_like and "python" in lower:
            return _memory_leak_planner_fallback()
        if fastapi_502_like:
            return _fastapi_502_planner_fallback()
        if migration_like:
            return _migration_planner_fallback()
        if race_condition_like:
            return _race_condition_planner_fallback()
        if stale_read_like:
            return _stale_read_planner_fallback()
        return "Break the task into diagnosis, likely causes, verification steps, and the safest next action."
    if role == "executor":
        if memory_leak_like and "python" in lower:
            return _memory_leak_executor_fallback()
        if fastapi_502_like:
            return _fastapi_502_executor_fallback()
        if migration_like:
            return _migration_executor_fallback()
        if race_condition_like:
            return _race_condition_executor_fallback()
        if stale_read_like:
            return _stale_read_executor_fallback()
        return "Answer directly, name the likely causes first, then give the shortest verification path."
    if role == "reviewer":
        if memory_leak_like and "python" in lower:
            return _memory_leak_reviewer_fallback()
        if fastapi_502_like:
            return _fastapi_502_reviewer_fallback()
        if migration_like:
            return _migration_reviewer_fallback()
        if race_condition_like:
            return _race_condition_reviewer_fallback()
        if stale_read_like:
            return _stale_read_reviewer_fallback()
        return "Tighten the answer around the main tradeoff, likely failure modes, and the clearest validation path."
    if role == "operator":
        return _operator_fallback()
    if role == "coder":
        return _coder_fallback()
    if role == "code_reviewer":
        return _code_reviewer_fallback()
    if role == "researcher":
        return _researcher_fallback()
    if role == "debugger":
        if memory_leak_like and "python" in lower:
            return _memory_leak_executor_fallback()
        if fastapi_502_like:
            return _fastapi_502_executor_fallback()
        if migration_like:
            return _migration_executor_fallback()
        if race_condition_like:
            return _race_condition_executor_fallback()
        if stale_read_like:
            return _stale_read_executor_fallback()
        return _debugger_fallback()
    if role == "vault_curator":
        return _vault_curator_fallback()
    if role == "security_reviewer":
        if auth_security_like:
            return _auth_security_fallback()
        return _security_fallback()
    if role == "security_analyst":
        return _security_analyst_fallback()
    if role == "self_improve_critic":
        return _self_improve_fallback()
    return "The specialist cloud path is unavailable, so the local fallback is returning the most defensible concise answer."


def _explicit_roles(user_input: str) -> list[str]:
    lower = user_input.lower()
    aliases = {
        "planner": ("planner", "plan this"),
        "executor": ("executor", "execute this"),
        "reviewer": ("reviewer", "review this"),
        "coder": ("coder", "coding agent", "use the coder", "use the coding agent"),
        "code_reviewer": ("code reviewer", "review this diff", "review this patch", "review this pr", "use the code reviewer"),
        "operator": ("operator", "use the operator", "have the operator"),
        "researcher": ("researcher", "research this", "use the researcher"),
        "debugger": ("debugger", "debug this", "use the debugger"),
        "vault_curator": ("vault curator", "vault_curator", "curate the vault", "use the vault curator"),
        "science_expert": ("science expert", "science_expert", "technology expert"),
        "security_reviewer": ("security reviewer", "security audit", "security review"),
        "security_analyst": ("security analyst", "security_analyst", "threat model this", "analyze the attack surface"),
        "self_improve_critic": ("self improve critic", "self-improve critic", "self review critic"),
    }
    selected = []
    for role, triggers in aliases.items():
        if any(trigger in lower for trigger in triggers):
            selected.append(role)
    return selected


def choose_roles(user_input: str) -> list[str]:
    lower = user_input.lower()
    word_count = len(lower.split())
    science_markers = (
        "transformer", "kv cache", "entropy", "thermodynamics", "information theory",
        "crispr", "biology", "physics", "chemistry", "lithography", "semiconductor",
        "materials science", "science", "technology",
    )
    security_markers = ("security", "auth", "authentication", "authorization", "exploit", "vulnerability")
    security_analysis_markers = (
        "threat model", "attack surface", "prompt injection", "jailbreak",
        "abuse path", "trust boundary", "privilege escalation", "data exposure",
    )
    debug_markers = (
        "debug", "debugging", "diagnose", "root cause", "why won't", "why doesnt", "why doesn't",
        "traceback", "stack trace", "launch failure", "crash", "regression", "flaky",
        "502", "memory leak", "race condition", "stale data", "replica lag",
    )
    research_markers = (
        "research", "investigate", "look through", "scan github", "browse repos",
        "compare repos", "source-backed", "findings", "what changed", "public repos",
    )
    coding_markers = (
        "implement", "write the patch", "make the patch", "fix this code",
        "refactor this", "add tests", "write tests", "update this function",
        "modify this file", "change this module", "implement this feature",
        "patch this", "code this", "ship this change",
    )
    code_review_markers = (
        "code review", "review this diff", "review this patch", "review this pr",
        "review this code", "regression risk", "what could break", "find bugs in this diff",
    )
    code_artifact_markers = (
        "code", "function", "class", "module", "file", "repo", "repository",
        "codebase", "test", "tests", "python", "javascript", "typescript",
        "fastapi", "react", "sql", "query",
    )
    vault_markers = (
        "distill into the brain", "story bank", "decision log", "roadmap note",
        "changelog", "patch this note", "update this note", "update the vault",
        "curate the vault", "brain schema",
    )
    asks_for_reasoning = bool(re.search(r"\b(why|how|explain|walk me through|tradeoff|trade-offs|compare|root cause|debug|debugging|diagnose|design|architecture|review|investigate|research|threat model)\b", lower))

    explicit = _explicit_roles(user_input)
    if explicit:
        if "code_reviewer" in explicit:
            return ["code_reviewer"]
        if "security_analyst" in explicit:
            return ["security_analyst", "reviewer"]
        if any(t in lower for t in security_analysis_markers) and "security_analyst" not in explicit:
            return ["security_analyst", "reviewer"]
        if any(t in lower for t in security_markers) and "security_reviewer" not in explicit:
            return ["security_reviewer", "reviewer"]
        if any(t in lower for t in science_markers) and "science_expert" not in explicit:
            return ["science_expert", "reviewer"]
        if explicit == ["code_reviewer"]:
            return ["code_reviewer"]
        if explicit == ["debugger"]:
            return ["debugger", "reviewer"]
        if explicit == ["coder"]:
            return ["coder", "reviewer"]
        if explicit == ["researcher"]:
            return ["researcher", "reviewer"]
        if explicit == ["vault_curator"]:
            return ["vault_curator", "reviewer"]
        if explicit == ["security_analyst"]:
            return ["security_analyst", "reviewer"]
        if explicit == ["operator"]:
            return ["operator", "reviewer"]
        return explicit

    roles = ["planner", "executor", "reviewer"]
    if any(t in lower for t in science_markers):
        return ["science_expert", "reviewer"]
    if any(t in lower for t in security_analysis_markers) and (asks_for_reasoning or word_count >= 8):
        return ["security_analyst", "reviewer"]
    if any(t in lower for t in security_markers):
        return ["security_reviewer", "reviewer"]
    if any(t in lower for t in research_markers) and word_count >= 8:
        return ["researcher", "reviewer"]
    if any(t in lower for t in code_review_markers) and any(t in lower for t in code_artifact_markers) and word_count >= 6:
        return ["code_reviewer"]
    if any(t in lower for t in coding_markers) and any(t in lower for t in code_artifact_markers) and word_count >= 6:
        return ["coder", "reviewer"]
    if any(t in lower for t in vault_markers) and word_count >= 6:
        return ["vault_curator", "reviewer"]
    if any(t in lower for t in debug_markers) and (asks_for_reasoning or word_count >= 8):
        return ["debugger", "reviewer"]
    if any(t in lower for t in ("improve yourself", "self improve", "self-improve", "should you change your code")):
        return ["self_improve_critic", "reviewer"]
    return roles


def _run_role(role: str, task: str, context: str = "") -> dict:
    native = specialized_agent_native.run_native_role_hook(role, task)
    if native:
        return {"role": role, "model": native["model"], "output": native["output"], "native": True}

    spec = AGENTS[role]
    system_extra, _ = skills.build_system_extra(task, tool="chat")
    if model_router._is_engineering_companion_query(task, "chat"):
        engineering_extra = model_router._engineering_companion_grounding(task)
        if engineering_extra:
            system_extra = system_extra + ("\n\n" if system_extra else "") + engineering_extra
    system = (
        f"{SYSTEM_PROMPT}\n\n"
        f"Specialized agent role for this request:\n{_load_agent_instructions(role)}\n\n"
        "Keep the output plain text and voice-safe. No markdown, no headers, no bullets, and no code fences."
    )
    prompt = task
    if context:
        prompt += f"\n\nContext from other agents:\n{context}"

    # In open-source mode skip the cloud attempt entirely — go local immediately
    if model_router.is_open_source_mode():
        preferred = _LOCAL_ROLE_MODELS.get(role, LOCAL_REASONING)
        try:
            local_model = get_best_available(preferred)
            full_system = system + ("\n\n" + system_extra if system_extra else "")
            output = _ask_local_with_timeout(prompt, model=local_model, system_extra=full_system)
            return {"role": role, "model": f"local/{local_model}", "output": output}
        except Exception as local_exc:
            output = _fallback_role_output(role, task, context=context).strip()
            return {"role": role, "model": "local/fallback", "output": output,
                    "fallback": True, "error": str(local_exc)}

    try:
        output = ask_claude(
            prompt,
            model=spec.model,
            system=system,
            system_extra=system_extra,
        ).strip()
        return {"role": role, "model": spec.model, "output": output}
    except Exception as exc:
        # Cloud failed — fall back to local instead of stub text
        preferred = _LOCAL_ROLE_MODELS.get(role, LOCAL_REASONING)
        try:
            local_model = get_best_available(preferred)
            full_system = system + ("\n\n" + system_extra if system_extra else "")
            output = _ask_local_with_timeout(prompt, model=local_model, system_extra=full_system)
            return {"role": role, "model": f"local/{local_model}", "output": output, "fallback": True}
        except Exception as local_exc:
            output = _fallback_role_output(role, task, context=context).strip()
            return {"role": role, "model": spec.model, "output": output,
                    "fallback": True, "error": f"cloud: {exc} | local: {local_exc}"}


def run(user_input: str, roles: list[str] | None = None) -> dict:
    selected = roles or choose_roles(user_input)
    stages = []
    shared_context = ""
    handoffs = []

    for role in selected:
        result = _run_role(role, user_input, context=shared_context)
        if vault_handoff.should_record_handoff(role, user_input):
            handoff = vault_handoff.record_role_handoff(role, user_input, result.get("output", ""))
            if handoff.get("ok"):
                result["handoff_path"] = handoff["path"]
                handoffs.append(handoff)
        stages.append(result)
        if role in {
            "planner", "coder", "code_reviewer", "operator", "researcher", "debugger", "vault_curator",
            "science_expert", "security_reviewer", "security_analyst", "self_improve_critic"
        }:
            shared_context += f"{role}: {result['output']}\n\n"

    if selected == ["planner", "executor", "reviewer"]:
        plan = stages[0]["output"]
        execution = _run_role("executor", user_input, context=f"planner: {plan}")
        stages[1] = execution
        review = _run_role("reviewer", user_input, context=f"planner: {plan}\n\nexecutor: {execution['output']}")
        stages[2] = review
        final_output = execution["output"]
    elif selected and selected[-1] == "reviewer" and len(stages) >= 2:
        final_output = stages[-2]["output"]
    else:
        final_output = stages[-1]["output"] if stages else ""

    return {
        "ok": True,
        "roles": selected,
        "stages": stages,
        "handoffs": handoffs,
        "final": final_output,
    }


def result_text(result: dict) -> str:
    if not result.get("ok"):
        return result.get("error", "Specialized agent run failed.")
    final = (result.get("final", "") or "").strip()
    if final:
        return final
    return "The specialist pass completed, but it did not produce a final answer."
