"""
Curated intake for external agent/runtime repos.

This keeps "what can Jarvis borrow?" separate from "install this dependency".
"""

from __future__ import annotations

from typing import Any


PATTERNS: tuple[dict[str, Any], ...] = (
    {
        "id": "agentic-stack",
        "name": "agentic-stack",
        "category": "portable-brain",
        "source_url": "https://github.com/codejunkie99/agentic-stack",
        "verdict": "adapt",
        "useful_patterns": [
            "portable .agent/ bridge folder shared by multiple coding-agent harnesses",
            "four memory layers with retention policies: working, episodic, semantic, personal",
            "host-agent review tools for staged candidate lessons before graduation",
            "recall-before-action hook for deployments, migrations, debugging, refactors, and timestamp work",
        ],
        "jarvis_seams": [
            "AGENTS.md",
            "semantic_memory.py",
            "skills/index.json",
            "vault/wiki/brain/79 Local Skill Loop.md",
            "vault/wiki/brain/83 External Agent Pattern Intake.md",
        ],
        "risks": [
            "do not let a foreign installer overwrite Jarvis's AGENTS.md, CLAUDE.md, or skill registry",
            "keep graduated lessons review-gated; no unattended semantic-memory mutation",
            "treat .agent/ as an export/compatibility surface before making it Jarvis's canonical brain",
        ],
    },
    {
        "id": "gbrain",
        "name": "GBrain",
        "category": "memory",
        "source_url": "https://github.com/garrytan/gbrain",
        "verdict": "adapt",
        "useful_patterns": [
            "dream-cycle maintenance for overnight consolidation",
            "entity enrichment for people, companies, meetings, and notes",
            "citation repair instead of ungrounded memory growth",
        ],
        "jarvis_seams": [
            "vault/wiki/brain/93 Vault Maintenance.md",
            "vault.py",
            "semantic_memory.py",
            "task_runtime.py",
        ],
        "risks": [
            "do not replace Jarvis's plain-markdown vault contract with a new brain store",
            "avoid importing private-life ingestion defaults without explicit consent gates",
        ],
    },
    {
        "id": "multica",
        "name": "Multica",
        "category": "managed-agents",
        "source_url": "https://github.com/multica-ai/multica",
        "verdict": "adapt",
        "useful_patterns": [
            "agents as issue assignees with profiles and lifecycle status",
            "runtime inventory for local CLIs and daemons",
            "progress streaming and blocker reporting",
            "skills compounding from completed work",
        ],
        "jarvis_seams": [
            "task_runtime.py",
            "jarvis_cli.py",
            "specialized_agents.py",
            "skills/index.json",
        ],
        "risks": [
            "do not add a cloud-first project-management dependency to the local core",
            "keep workspace isolation and approval gates stronger than a generic teammate board",
        ],
    },
    {
        "id": "claude-code-best-practice",
        "name": "Claude Code Best Practice",
        "category": "coding-workflow",
        "source_url": "https://github.com/shanraisshan/claude-code-best-practice",
        "verdict": "adapt",
        "useful_patterns": [
            "session memory and reusable command packs",
            "project-specific hooks that preserve setup knowledge",
            "explicit coding-agent workflows instead of giant prompts",
        ],
        "jarvis_seams": [
            "context_budget.py",
            "agents/skill_builder.md",
            "jarvis_cli.py",
            "vault/wiki/brain/82 Context Budget Discipline.md",
        ],
        "risks": [
            "do not copy prompt packs wholesale; translate only repeated local workflows",
            "avoid auto-hooks that mutate code without Jarvis approval gates",
        ],
    },
    {
        "id": "openmythos",
        "name": "OpenMythos",
        "category": "model-research",
        "source_url": "https://github.com/kyegomez/OpenMythos",
        "verdict": "watch",
        "useful_patterns": [
            "recurrent-depth transformer framing for compute-adaptive reasoning",
            "looped reasoning depth as a local-model evaluation idea",
            "MoE and attention variants as research notes, not product dependencies",
        ],
        "jarvis_seams": [
            "local_runtime/local_model_eval.py",
            "local_runtime/local_model_benchmark.py",
            "vault/wiki/brain/78 AI Runtime Agent Engineering Principles.md",
        ],
        "risks": [
            "do not treat speculative Claude Mythos claims as facts",
            "do not add training architecture experiments to the desktop runtime path",
        ],
    },
    {
        "id": "scrapling",
        "name": "Scrapling",
        "category": "web-retrieval",
        "source_url": "https://github.com/D4Vinci/Scrapling",
        "verdict": "gate",
        "useful_patterns": [
            "adaptive selectors for brittle page extraction",
            "CLI/MCP-style web retrieval as a separate tool lane",
            "structured scrape results instead of full-page dumps",
        ],
        "jarvis_seams": [
            "browser.py",
            "source_ingest.py",
            "safety_permissions.py",
            "context_budget.py",
        ],
        "risks": [
            "stealth or bot-evasion modes require explicit legal/ToS review",
            "browser automation must respect robots.txt, authentication boundaries, and user consent",
        ],
    },
    {
        "id": "decepticon",
        "name": "Decepticon",
        "category": "security",
        "source_url": "https://github.com/PurpleAILAB/Decepticon",
        "verdict": "defensive-only",
        "useful_patterns": [
            "rules of engagement before any security action",
            "sandbox isolation between management and operational networks",
            "findings-to-defense feedback loop",
            "phase-specific agents with fresh context windows",
        ],
        "jarvis_seams": [
            "safety_permissions.py",
            "agents/security_reviewer.md",
            "vault/wiki/brain/77 Threat Modeling Security Thinking.md",
            "task_runtime.py",
        ],
        "risks": [
            "do not integrate autonomous exploitation or kill-chain execution",
            "security workflows must require written authorization, scope, and sandboxing",
        ],
    },
)


def list_patterns(category: str = "") -> list[dict[str, Any]]:
    normalized = (category or "").strip().lower()
    if not normalized:
        return [dict(item) for item in PATTERNS]
    return [dict(item) for item in PATTERNS if item["category"] == normalized or item["id"] == normalized]


def pattern_status() -> dict[str, Any]:
    counts: dict[str, int] = {}
    for item in PATTERNS:
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
    return {
        "ok": True,
        "pattern_count": len(PATTERNS),
        "verdict_counts": counts,
        "patterns": list_patterns(),
    }


def summary_text(category: str = "") -> str:
    patterns = list_patterns(category)
    if not patterns:
        return f"No external agent pattern found for {category}."
    lines = [
        "External agent pattern intake: borrow operating patterns, not dependencies, until a local safety review passes.",
        "",
    ]
    for item in patterns:
        useful = "; ".join(item["useful_patterns"][:2])
        seam = ", ".join(item["jarvis_seams"][:2])
        lines.append(f"- {item['name']} [{item['verdict']}]: {useful}. Jarvis seams: {seam}.")
    lines.extend(
        [
            "",
            "Default rule: adapt memory, skills, lifecycle, and sandbox patterns; gate browser/scraping; keep offensive security defensive-only.",
        ]
    )
    return "\n".join(lines)
