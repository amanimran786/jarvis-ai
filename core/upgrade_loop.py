"""
Local-first continuous upgrade loop for Jarvis.

This module turns research notes, audit findings, and operator briefs into a
measurable, review-gated upgrade backlog. It deliberately does not browse the
web, call cloud models, mutate code, or dispatch agents. The output is a
preview-only work order that Jarvis Manager or a human can approve later.

Loop shape:
  research/findings -> score candidates -> preview local work orders -> ledger

The ledger makes the loop measurable over time: how many signals appeared,
which categories dominate, what required review, and which proposals were
accepted/rejected by a human.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import runtime_state
from core.work_order import preview_work_order


DEFAULT_WATCHLIST = [
    {
        "category": "local_runtime",
        "title": "Ollama local agent integrations",
        "source_url": "https://docs.ollama.com/llms.txt",
        "why": "Track local model, OpenClaw, Hermes, structured output, and context-window changes.",
    },
    {
        "category": "apple_on_device",
        "title": "Apple Foundation Models framework",
        "source_url": "https://developer.apple.com/documentation/foundationmodels/",
        "why": "Track on-device model, guided generation, tool calling, and adapter-training options.",
    },
    {
        "category": "training",
        "title": "MLX fine-tuning and GRPO",
        "source_url": "https://github.com/ARahim3/mlx-tune",
        "why": "Track Apple Silicon SFT/DPO/GRPO training improvements for local Jarvis models.",
    },
    {
        "category": "evaluation",
        "title": "Agent evaluation and observability",
        "source_url": "local:pipeline_audit+eval_trace_score",
        "why": "Track step-level tracing, tool correctness, cost, safety, and regression gates.",
    },
]


_CATEGORY_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("security", ("security", "sandbox", "rbac", "secret", "credential", "vuln", "exploit")),
    ("training", ("grpo", "dpo", "orpo", "lora", "finetune", "fine-tune", "mlx")),
    ("local_runtime", ("ollama", "hermes", "openclaw", "local model", "context window", "mtp")),
    ("apple_on_device", ("apple foundation", "foundation models", "apfel", "on-device", "macos 26")),
    ("memory", ("mem0", "qdrant", "fastembed", "obsidian", "vault", "memory")),
    ("evaluation", ("eval", "benchmark", "trace", "metric", "score", "verifier", "golden")),
    ("frontend", ("ui", "dashboard", "ux", "panel", "interface")),
]

_RISK_TERMS = (
    "api key", "credential", "secret", "token", "delete", "deploy", "git push",
    "production", "external api", "cloud", "callback", "exploit", "intrusive",
    "malware", "sudo", "submit", "third-party",
)
_IMPACT_TERMS = (
    "2x", "latency", "speed", "token", "cost", "quality", "accuracy", "routing",
    "local-first", "security", "privacy", "offline", "p95", "benchmark",
)
_LOCAL_TERMS = (
    "local", "on-device", "offline", "ollama", "mlx", "apple", "macos",
    "qdrant", "fastembed", "sandbox", "worktree",
)
_MEASURE_TERMS = (
    "test", "benchmark", "metric", "eval", "trace", "latency", "tokens",
    "p95", "pass rate", "regression", "score",
)
_LOW_EFFORT_TERMS = ("one line", "config", "install", "pip install", "pull", "env var")
_HIGH_EFFORT_TERMS = ("rewrite", "architecture", "packaging", "migration", "new subsystem")


@dataclass
class UpgradeSignal:
    title: str
    summary: str
    source: str = "manual"
    source_url: str = ""
    category: str = ""
    evidence: list[str] = field(default_factory=list)


@dataclass
class UpgradeCandidate:
    candidate_id: str
    title: str
    summary: str
    category: str
    source: str
    source_url: str
    score: int
    priority: int
    risk_level: str
    requires_review: bool
    recommended_agent: str
    scorecard: dict[str, int]
    work_order: dict[str, Any]
    status: str = "proposed"


def ledger_path() -> Path:
    override = os.getenv("JARVIS_UPGRADE_LEDGER_PATH", "").strip()
    if override:
        return Path(override)
    return runtime_state.writable_data_path("upgrade_loop", "upgrade_cycles.jsonl")


def ticket_path() -> Path:
    override = os.getenv("JARVIS_UPGRADE_TICKET_PATH", "").strip()
    if override:
        return Path(override)
    return runtime_state.writable_data_path("upgrade_loop", "upgrade_tickets.jsonl")


def source_state_path() -> Path:
    override = os.getenv("JARVIS_UPGRADE_SOURCE_STATE_PATH", "").strip()
    if override:
        return Path(override)
    return runtime_state.writable_data_path("upgrade_loop", "source_state.json")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _candidate_id(title: str, source: str) -> str:
    seed = f"{source}:{title}".encode("utf-8", errors="ignore")
    return "upg_" + uuid.uuid5(uuid.NAMESPACE_URL, seed.decode("utf-8", errors="ignore")).hex[:12]


def _sha256(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def _detect_category(text: str) -> str:
    lower = text.lower()
    for category, hints in _CATEGORY_HINTS:
        if any(h in lower for h in hints):
            return category
    return "general"


def _risk_level(text: str) -> str:
    lower = text.lower()
    hits = sum(1 for term in _RISK_TERMS if term in lower)
    if hits >= 2:
        return "high"
    if hits == 1:
        return "medium"
    return "low"


def _count_terms(text: str, terms: tuple[str, ...]) -> int:
    lower = text.lower()
    return sum(1 for term in terms if term in lower)


def _score_signal(signal: UpgradeSignal) -> tuple[int, int, str, dict[str, int]]:
    text = f"{signal.title}\n{signal.summary}\n{' '.join(signal.evidence)}"
    risk = _risk_level(text)
    impact = min(10, 3 + _count_terms(text, _IMPACT_TERMS))
    local_fit = min(10, 2 + _count_terms(text, _LOCAL_TERMS))
    measurable = min(10, 2 + _count_terms(text, _MEASURE_TERMS))
    effort = 5
    if _count_terms(text, _LOW_EFFORT_TERMS):
        effort -= 2
    if _count_terms(text, _HIGH_EFFORT_TERMS):
        effort += 2
    effort = max(1, min(10, effort))
    risk_penalty = {"low": 0, "medium": 2, "high": 4}[risk]
    score = max(0, min(100, impact * 3 + local_fit * 2 + measurable * 2 + (11 - effort) - risk_penalty))
    priority = max(1, min(10, round(score / 10)))
    return score, priority, risk, {
        "impact": impact,
        "local_fit": local_fit,
        "measurable": measurable,
        "effort": effort,
        "risk_penalty": risk_penalty,
    }


def _agent_for_category(category: str, risk: str) -> str:
    if risk == "high" or category == "security":
        return "security_reviewer"
    return {
        "frontend": "frontend_designer",
        "memory": "memory_librarian",
        "evaluation": "qa_tester",
        "training": "backend_engineer",
        "local_runtime": "backend_engineer",
        "apple_on_device": "backend_engineer",
    }.get(category, "researcher")


def _work_order_for(signal: UpgradeSignal, *, category: str, priority: int,
                    risk: str, agent: str) -> dict[str, Any]:
    needs_review = risk != "low" or agent in {"security_reviewer", "devops_release"}
    tasks: list[dict[str, Any]] = [
        {
            "title": f"Validate upgrade signal: {signal.title}",
            "description": (
                f"Validate this Jarvis upgrade signal using local repo evidence first. "
                f"Source label: {signal.source}. Source reference is stored in context metadata. "
                f"Summarize whether it fits Jarvis and define the metric to improve. "
                f"Signal summary: {signal.summary}"
            ),
            "agent": "researcher",
            "priority": priority,
            "needs_security_review": False,
            "context": {
                "upgrade_candidate": signal.title,
                "upgrade_category": category,
                "source_url": signal.source_url,
            },
        },
        {
            "title": f"Implement sandboxed upgrade: {signal.title}",
            "description": (
                "If validation passes, make the smallest repo-native change in an isolated "
                "worktree. Keep it local-first, add or update focused tests, and do not merge "
                "or push without human approval."
            ),
            "agent": agent if agent != "security_reviewer" else "backend_engineer",
            "priority": priority,
            "needs_security_review": needs_review,
            "context": {
                "upgrade_candidate": signal.title,
                "upgrade_category": category,
                "requires_human_review": needs_review,
            },
        },
        {
            "title": f"Measure upgrade result: {signal.title}",
            "description": (
                "Run focused tests or benchmarks that prove the upgrade improved the named "
                "metric without regressing sandbox, audit, or local-first behavior. Report "
                "commands, pass/fail, residual risk, and rollback notes."
            ),
            "agent": "qa_tester",
            "priority": priority,
            "needs_security_review": False,
            "context": {
                "upgrade_candidate": signal.title,
                "upgrade_category": category,
            },
        },
    ]
    if risk != "low":
        tasks.append({
            "title": f"Security review upgrade: {signal.title}",
            "description": (
                "Review the proposed upgrade for data egress, secrets, filesystem writes, "
                "network access, model routing, and rollback safety before any merge."
            ),
            "agent": "security_reviewer",
            "priority": priority,
            "needs_security_review": True,
            "context": {"upgrade_candidate": signal.title, "upgrade_category": category},
        })
    return preview_work_order(json.dumps({"tasks": tasks}), source="upgrade_loop")


def _signal_from_mapping(item: dict[str, Any], *, source: str) -> UpgradeSignal:
    title = str(item.get("title") or item.get("name") or item.get("upgrade") or "Upgrade signal")[:160]
    summary = str(item.get("summary") or item.get("description") or item.get("finding") or item.get("why") or title)
    category = str(item.get("category") or "").strip()
    evidence_raw = item.get("evidence")
    evidence = [str(x) for x in evidence_raw] if isinstance(evidence_raw, list) else []
    return UpgradeSignal(
        title=title,
        summary=summary,
        source=str(item.get("source") or source),
        source_url=str(item.get("source_url") or item.get("url") or ""),
        category=category,
        evidence=evidence,
    )


def parse_signals(brief: str, *, source: str = "manual") -> list[UpgradeSignal]:
    """Parse JSON or bullet upgrade notes into normalized signals."""
    text = (brief or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        raw = data.get("signals") or data.get("findings") or data.get("upgrades") or data.get("tasks")
        if isinstance(raw, list):
            return [_signal_from_mapping(item, source=source) for item in raw if isinstance(item, dict)]
        return [_signal_from_mapping(data, source=source)]
    if isinstance(data, list):
        return [_signal_from_mapping(item, source=source) for item in data if isinstance(item, dict)]

    signals: list[UpgradeSignal] = []
    bullet = re.compile(r"^\s*(?:[-*]|\d+[.)])\s*(?P<body>.+?)\s*$")
    for line in text.splitlines():
        match = bullet.match(line)
        if not match:
            continue
        body = match.group("body").strip()
        if not body:
            continue
        title, _, summary = body.partition(":")
        if not summary:
            summary = title
        signals.append(UpgradeSignal(title=title.strip()[:160], summary=summary.strip(), source=source))
    if signals:
        return signals
    return [UpgradeSignal(title=text[:120], summary=text, source=source)]


def build_candidates(signals: list[UpgradeSignal], *, limit: int = 10) -> list[UpgradeCandidate]:
    candidates: list[UpgradeCandidate] = []
    for signal in signals:
        category = signal.category or _detect_category(f"{signal.title}\n{signal.summary}")
        score, priority, risk, scorecard = _score_signal(signal)
        agent = _agent_for_category(category, risk)
        work_order = _work_order_for(signal, category=category, priority=priority, risk=risk, agent=agent)
        candidates.append(UpgradeCandidate(
            candidate_id=_candidate_id(signal.title, signal.source),
            title=signal.title,
            summary=signal.summary,
            category=category,
            source=signal.source,
            source_url=signal.source_url,
            score=score,
            priority=priority,
            risk_level=risk,
            requires_review=work_order["review_required_count"] > 0,
            recommended_agent=agent,
            scorecard=scorecard,
            work_order=work_order,
        ))
    candidates.sort(key=lambda c: (c.score, c.priority), reverse=True)
    return candidates[: max(1, int(limit))]


def _metrics(candidates: list[UpgradeCandidate]) -> dict[str, Any]:
    by_category = Counter(c.category for c in candidates)
    by_agent = Counter(c.recommended_agent for c in candidates)
    by_risk = Counter(c.risk_level for c in candidates)
    review_required = sum(1 for c in candidates if c.requires_review)
    return {
        "candidate_count": len(candidates),
        "review_required_count": review_required,
        "by_category": dict(sorted(by_category.items())),
        "by_agent": dict(sorted(by_agent.items())),
        "by_risk": dict(sorted(by_risk.items())),
        "top_score": max((c.score for c in candidates), default=0),
        "average_score": round(sum(c.score for c in candidates) / len(candidates), 2) if candidates else 0,
    }


def append_record(record: dict[str, Any], path: Path | None = None) -> None:
    path = path or ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


def _fetch_url(url: str, *, timeout: float = 10.0) -> tuple[str, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "JarvisUpgradeLoop/1.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get("content-type", "")
            raw = response.read(300_000)
        return raw.decode("utf-8", errors="replace"), content_type
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return "", f"error:{exc}"


def _load_source_state(path: Path | None = None) -> dict[str, Any]:
    path = path or source_state_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_source_state(state: dict[str, Any], path: Path | None = None) -> None:
    path = path or source_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def fetch_watchlist_signals(*, state_path: Path | None = None, force: bool = False) -> tuple[list[UpgradeSignal], dict[str, Any]]:
    """Fetch configured public watchlist sources only when explicitly requested.

    This is network-capable by design, but never calls LLMs and never executes
    source content. A changed digest becomes an upgrade signal for review.
    """
    state = _load_source_state(state_path)
    signals: list[UpgradeSignal] = []
    source_reports: dict[str, Any] = {}
    for item in DEFAULT_WATCHLIST:
        url = str(item.get("source_url", ""))
        title = str(item.get("title", "Upgrade source"))
        if url.startswith("local:"):
            continue
        body, content_type = _fetch_url(url)
        digest = _sha256(body) if body else ""
        previous = str(state.get(url, {}).get("digest", ""))
        changed = bool(digest and (force or digest != previous))
        source_reports[url] = {
            "title": title,
            "ok": bool(body),
            "changed": changed,
            "digest": digest[:16],
            "content_type": content_type,
        }
        if not body:
            continue
        state[url] = {"digest": digest, "checked_at": _now(), "title": title}
        if changed:
            signals.append(UpgradeSignal(
                title=f"Watchlist changed: {title}",
                summary=str(item.get("why") or title),
                source="watchlist_fetch",
                source_url=url,
                category=str(item.get("category", "")),
                evidence=[f"content_type={content_type}", f"digest={digest[:16]}"],
            ))
    _save_source_state(state, state_path)
    return signals, source_reports


def read_tickets(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or ticket_path()
    if not path.exists():
        return []
    tickets: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                tickets.append(obj)
        except json.JSONDecodeError:
            continue
    return tickets


def create_tickets(candidates: list[UpgradeCandidate], *, path: Path | None = None,
                   auto_promote: bool = False, min_score: int = 25) -> list[dict[str, Any]]:
    """Append durable local upgrade tickets. Promotion means backlog promotion,
    not code merge, push, deploy, or execution."""
    path = path or ticket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_ids = {str(t.get("candidate_id", "")) for t in read_tickets(path)}
    created: list[dict[str, Any]] = []
    with open(path, "a", encoding="utf-8") as f:
        for candidate in candidates:
            if candidate.candidate_id in existing_ids:
                continue
            status = "promoted_to_backlog" if (
                auto_promote and candidate.score >= min_score and candidate.risk_level == "low"
            ) else "proposed"
            ticket = {
                "type": "upgrade_ticket",
                "ticket_id": "ticket_" + uuid.uuid4().hex[:12],
                "candidate_id": candidate.candidate_id,
                "title": candidate.title,
                "category": candidate.category,
                "score": candidate.score,
                "priority": candidate.priority,
                "risk_level": candidate.risk_level,
                "requires_review": candidate.requires_review,
                "status": status,
                "created_at": _now(),
                "source": candidate.source,
                "source_url": candidate.source_url,
                "work_order": candidate.work_order,
            }
            f.write(json.dumps(ticket, sort_keys=True, ensure_ascii=False) + "\n")
            created.append(ticket)
    return created


def sandbox_smoke(cmd: list[str] | None = None) -> dict[str, Any]:
    """Run a local smoke command for the upgrade loop itself.

    This does not test arbitrary downloaded content. It proves the local ticket
    and safety plumbing still composes with work-order, sandbox, and audit tests.
    """
    cmd = cmd or [
        "./venv/bin/python", "-m", "pytest",
        "tests/test_upgrade_loop.py",
        "tests/test_work_order.py",
        "tests/test_task_runtime_sandbox.py",
        "-q",
    ]
    started = _now()
    try:
        proc = subprocess.run(cmd, cwd=str(runtime_state.source_root()), capture_output=True,
                              text=True, timeout=120, check=False)
        ok = proc.returncode == 0
        return {
            "ok": ok,
            "started_at": started,
            "finished_at": _now(),
            "cmd": cmd,
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-4000:],
            "stderr_tail": proc.stderr[-4000:],
        }
    except Exception as exc:
        return {
            "ok": False,
            "started_at": started,
            "finished_at": _now(),
            "cmd": cmd,
            "returncode": -1,
            "stdout_tail": "",
            "stderr_tail": str(exc),
        }


def run_cycle(briefs: list[str] | str, *, source: str = "manual", limit: int = 10,
              append: bool = True, path: Path | None = None,
              create_ticket_records: bool = False, auto_promote: bool = False,
              sandbox_test: bool = False) -> dict[str, Any]:
    """Run one upgrade-loop pass and optionally append it to the ledger."""
    if isinstance(briefs, str):
        briefs = [briefs]
    signals: list[UpgradeSignal] = []
    for brief in briefs:
        signals.extend(parse_signals(brief, source=source))
    candidates = build_candidates(signals, limit=limit) if signals else []
    record = {
        "type": "upgrade_cycle",
        "cycle_id": "cycle_" + uuid.uuid4().hex[:12],
        "ts": _now(),
        "source": source,
        "signal_count": len(signals),
        "metrics": _metrics(candidates),
        "candidates": [asdict(c) for c in candidates],
        "execute": False,
        "note": "Preview only. Human approval is required before dispatch, merge, push, or deploy.",
    }
    if create_ticket_records:
        tickets = create_tickets(candidates, auto_promote=auto_promote)
        record["tickets_created"] = len(tickets)
        record["tickets"] = tickets
    if sandbox_test:
        record["sandbox_smoke"] = sandbox_smoke()
    if append:
        append_record(record, path)
    return record


def record_decision(candidate_id: str, decision: str, *, reason: str = "",
                    actor: str = "operator", path: Path | None = None) -> dict[str, Any]:
    """Append a human decision for later metrics. Does not execute anything."""
    normalized = (decision or "").strip().lower()
    if normalized not in {"accepted", "rejected", "deferred", "needs_more_research"}:
        normalized = "deferred"
    record = {
        "type": "upgrade_decision",
        "ts": _now(),
        "candidate_id": candidate_id,
        "decision": normalized,
        "reason": reason,
        "actor": actor,
    }
    append_record(record, path)
    return record


def read_records(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or ledger_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict):
                out.append(obj)
        except json.JSONDecodeError:
            out.append({"type": "corrupt", "raw": raw})
    return out


def summarize(path: Path | None = None) -> dict[str, Any]:
    records = read_records(path)
    cycles = [r for r in records if r.get("type") == "upgrade_cycle"]
    decisions = [r for r in records if r.get("type") == "upgrade_decision"]
    candidates = [c for cycle in cycles for c in cycle.get("candidates", [])]
    decision_counts = Counter(str(d.get("decision", "")) for d in decisions)
    category_counts = Counter(str(c.get("category", "")) for c in candidates)
    return {
        "cycle_count": len(cycles),
        "candidate_count": len(candidates),
        "ticket_count": len(read_tickets()),
        "decision_count": len(decisions),
        "decisions": dict(sorted(decision_counts.items())),
        "by_category": dict(sorted(category_counts.items())),
        "latest_cycle_id": cycles[-1].get("cycle_id", "") if cycles else "",
        "latest_metrics": cycles[-1].get("metrics", {}) if cycles else {},
    }
