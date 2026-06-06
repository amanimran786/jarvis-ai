# Jarvis OS — Production Architecture

**Version:** 1.0 | **Author:** Jarvis Architecture Team | **Date:** 2026-06-06

This document is the ground truth for how Jarvis is built. Every decision here has a rationale and maps to actual code. Read this before touching the system.

---

## 1. System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│  TIER 0 — Human Operator (Aman)                                     │
│  Channels: Desktop UI, Voice, iMessage, Telegram (via OpenClaw),    │
│            Web HUD (mobile), CLI                                    │
└────────────────────────────┬────────────────────────────────────────┘
                             │  intent / approval / override
┌────────────────────────────▼────────────────────────────────────────┐
│  TIER 1 — Jarvis Manager (jarvis_manager.py)                        │
│                                                                     │
│  • Task decomposition          • Agent routing + selection          │
│  • Plan generation             • Context window budgeting           │
│  • Progress tracking           • Risk scoring                       │
│  • Approval gate control       • Memory coordination                │
│  • Review chain orchestration  • Failure recovery                   │
│                                                                     │
│  Model: glm-4.7-flash (local) → qwen3:30b (complex planning)       │
└──────┬──────────┬──────────┬──────────┬──────────┬─────────────────┘
       │          │          │          │          │
┌──────▼──┐ ┌────▼────┐ ┌───▼────┐ ┌──▼──────┐ ┌─▼───────────────┐
│ backend │ │frontend │ │security│ │research │ │ memory_librarian │
│_engineer│ │_designer│ │_review │ │         │ │                  │
└─────────┘ └─────────┘ └────────┘ └─────────┘ └─────────────────┘
       (13 specialist agents — see Section 3)

┌─────────────────────────────────────────────────────────────────────┐
│  INFRASTRUCTURE LAYER                                               │
│                                                                     │
│  Ollama (11434)     Redis Streams (6379)    PostgreSQL (5432)       │
│  glm-4.7-flash      Event bus / queues      Audit + memory          │
│  qwen3:8b           Agent state cache       RBAC + sessions         │
│  llava:7b           Rate limiting           Project memory          │
│                                                                     │
│  Qdrant (6333)      SQLite (local)          OpenClaw (18789)        │
│  Vector search      Task persistence        Multi-channel gateway   │
│  Semantic recall    Fast local ops          iMessage/Telegram/etc.  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 2. Architectural Decisions

### Why SQLite stays for tasks
Task writes are single-writer (task_runtime.py holds `_LOCK`). WAL mode gives concurrent reads. SQLite handles 1M tasks without issue. Migration cost to PostgreSQL exceeds the benefit for a single-user system.

### Why PostgreSQL for audit + memory
Memory writes come from multiple agents concurrently. PostgreSQL JSONB + GIN indexes outperform SQLite for semantic search queries. Audit logs need append-only with row-level security. These justify the dependency.

### Why Redis Streams (not Celery, not RQ)
Redis Streams persist messages until acknowledged. Consumer groups allow multiple agents to compete for tasks. Replay is free. The existing stack already uses Redis concepts (rate limiting); adding the client is trivial.

### Why not Kubernetes yet
Single M4 Pro Mac. Kubernetes overhead costs more than it saves below 10 concurrent agents. Docker Compose with named services gives service isolation without the complexity. Revisit at 50+ daily workflows.

---

## 3. Agent Specifications

### 3.0 Jarvis Manager (Tier 1)

**Purpose:** Decompose user intent into agent tasks, route work, track progress, coordinate memory, enforce approval gates.

**Model routing:** `glm-4.7-flash` for routing decisions; `qwen3:8b` for complex multi-step plans.

**Inputs:** Raw user intent string, active context window, memory snapshot, current task queue state.

**Outputs:** `TaskPlan` (ordered list of agent tasks with dependencies), approval decisions, memory write directives.

**Responsibilities:**
- Parse intent → subtasks using structured output
- Assign each subtask to the correct specialist agent
- Set `review_chain` on each task requiring validation
- Monitor task progress; escalate failures to human
- Synthesize agent outputs into a coherent final response
- Coordinate memory writes post-completion

**Approval gates the Manager enforces:**
- Any task touching external state (git push, email send, file delete)
- Any task estimated confidence < 0.74
- Any security finding marked HIGH or CRITICAL
- Any code deployment

---

### 3.1 backend_engineer

**Purpose:** Implement server-side features, APIs, data models, and system integrations in Python/FastAPI.

**Responsibilities:**
- Write and modify Python modules
- Implement FastAPI endpoints with full type hints and validation
- Design and migrate database schemas (SQLite, PostgreSQL)
- Write integration tests
- Review database query performance

**Inputs:** Task description, repo context (file list + relevant diffs), schema snapshots, API contracts.

**Outputs:** Code files with diff, test file, brief debrief with files touched.

**Allowed tools:**
```
read_file, write_file, run_shell (restricted), web_search,
run_tests, git_diff, git_status
```

**Restricted tools:**
```
git_push, send_email, send_message, deploy, delete_file (requires approval)
```

**Memory access:** `project` (read/write), `knowledge/engineering` (read)

**Review requirements:** QA Tester → Security Reviewer → Manager approval before merge.

**Failure handling:** On test failure, retry once with error context. On security finding, halt and escalate. Never commit without test pass.

---

### 3.2 frontend_designer

**Purpose:** Build UI components, responsive layouts, and interaction patterns.

**Responsibilities:**
- Implement PyQt6 desktop components
- Build web HUD features (HTML/CSS/JS in api.py root endpoint)
- Design component hierarchy and state management
- Ensure accessibility and responsive behavior

**Inputs:** Design spec or description, existing component inventory, style tokens.

**Outputs:** Component code, screenshot or wireframe description, change diff.

**Allowed tools:**
```
read_file, write_file, web_search, run_shell (npm/build only)
```

**Restricted tools:**
```
git_push, deploy, database_write
```

**Memory access:** `project` (read), `knowledge/design_system` (read/write)

**Review requirements:** UX Researcher review → Manager approval for any user-facing change.

**Failure handling:** On render failure, revert to last known-good component. Log failure to memory.

---

### 3.3 ux_researcher

**Purpose:** Analyze user needs, synthesize feedback, evaluate interaction quality, produce recommendations.

**Responsibilities:**
- Analyze usage patterns from logs
- Research industry UX patterns via web search
- Produce structured usability reports
- Score UI changes for friction reduction
- Maintain UX knowledge base in vault

**Inputs:** Feature description or question, usage data, comparative product list.

**Outputs:** UX report (markdown), recommendation list with priority scores, vault write.

**Allowed tools:**
```
web_search, read_file, vault_write, memory_lookup
```

**Restricted tools:**
```
write_file (code), run_shell, git_*, deploy
```

**Memory access:** `project` (read), `knowledge/ux` (read/write), `personal/preferences` (read)

**Review requirements:** Manager review for recommendations affecting product direction.

**Failure handling:** If web search unavailable, fall back to vault knowledge. Always cite sources.

---

### 3.4 security_reviewer

**Purpose:** Review code and configurations for vulnerabilities, model systems for threat vectors, enforce security policy.

**Responsibilities:**
- Static analysis of diffs for OWASP top 10
- Review prompt templates for injection vectors
- Audit tool permissions for privilege escalation
- Model threat scenarios for new features
- Maintain security findings in audit log

**Inputs:** Code diff, configuration change, or architecture proposal.

**Outputs:** Findings report (CRITICAL/HIGH/MEDIUM/LOW/INFO), remediation guidance, PASS/FAIL verdict.

**Allowed tools:**
```
read_file, web_search, run_shell (grep/static analysis only), memory_lookup
```

**Restricted tools:**
```
write_file (code), git_push, deploy, send_message
```

**Memory access:** `knowledge/security` (read/write), `audit_log` (write), `project` (read)

**Review requirements:** CRITICAL findings escalate directly to human. HIGH findings block merge.

**Failure handling:** Any exception in security review = FAIL verdict. Never silently pass on error.

---

### 3.5 qa_tester

**Purpose:** Write tests, execute test suites, reproduce bugs, verify correctness of agent outputs.

**Responsibilities:**
- Write pytest unit and integration tests
- Run existing test suites and report failures
- Reproduce bugs from issue descriptions
- Verify backend_engineer outputs against acceptance criteria
- Generate test coverage reports

**Inputs:** Code to test, acceptance criteria, existing test files.

**Outputs:** Test file, test run results, coverage delta, pass/fail verdict.

**Allowed tools:**
```
read_file, write_file (test files only), run_tests, run_shell (pytest only),
web_search (for testing patterns)
```

**Restricted tools:**
```
git_push, deploy, send_*, database_write (production)
```

**Memory access:** `project` (read), `knowledge/testing` (read/write)

**Review requirements:** Security Reviewer reviews test files touching auth/security paths.

**Failure handling:** Flaky test → mark skip with reason, escalate to backend_engineer. Never delete a failing test without approval.

---

### 3.6 researcher

**Purpose:** Deep web research, competitive analysis, technology evaluation, knowledge synthesis.

**Responsibilities:**
- Execute multi-query research pipelines (uses research.py)
- Synthesize sources into structured reports with citations
- Evaluate technologies against defined criteria
- Monitor competitive landscape for relevant domains
- Write findings to vault

**Inputs:** Research question or topic, scope (depth 1-5), optional domain constraints.

**Outputs:** Markdown research report with citations, source quality scores, vault write.

**Allowed tools:**
```
web_search, web_fetch, read_file, vault_write, memory_lookup
```

**Restricted tools:**
```
write_file (code), run_shell, git_*, send_*
```

**Memory access:** `knowledge/*` (read/write), `project` (read)

**Review requirements:** Manager review for research that will drive product decisions.

**Failure handling:** If < 3 quality sources found, flag uncertainty explicitly. Never fabricate citations.

---

### 3.7 devops_release

**Purpose:** Manage build pipelines, deployment orchestration, infrastructure configuration, release coordination.

**Responsibilities:**
- Build and package Jarvis.app (PyInstaller)
- Manage Docker Compose service definitions
- Run health checks pre/post deploy
- Coordinate release version bumping
- Monitor service health after deploy

**Inputs:** Release request, current version, build config, target environment.

**Outputs:** Build artifact, deployment report, rollback plan, health check results.

**Allowed tools:**
```
read_file, write_file (config/docker files), run_shell, git_status,
git_tag (not push), run_tests
```

**Restricted tools (require explicit human approval):**
```
git_push, docker_push, deploy_production, delete_volume
```

**Memory access:** `project` (read/write), `knowledge/infrastructure` (read/write)

**Review requirements:** QA Tester signs off on test pass → Security Reviewer → Human approval for production deploy.

**Failure handling:** Failed deploy → automatic rollback to last known-good. Page human immediately.

---

### 3.8 memory_librarian

**Purpose:** Maintain the Jarvis knowledge base: index, retrieve, curate, prune, and resolve conflicts across all memory layers.

**Responsibilities:**
- Index new documents and sessions into semantic memory
- Prune stale or contradictory facts per policy
- Resolve conflicts between memory layers
- Build and maintain Obsidian vault structure
- Generate memory health reports

**Inputs:** New content to index, pruning trigger, conflict report, or health check request.

**Outputs:** Index update confirmation, pruning report, conflict resolution diff, vault write.

**Allowed tools:**
```
read_file, vault_write, vault_read, memory_lookup, memory_write
```

**Restricted tools:**
```
run_shell, git_*, send_*, web_search (except for fact verification)
```

**Memory access:** `all layers` (read/write) — highest memory privilege of any agent.

**Review requirements:** Memory deletions require Manager approval. Vault structural changes require human approval.

**Failure handling:** Never delete without backup. On conflict: preserve both versions, flag for human resolution.

---

### 3.9 data_analyst

**Purpose:** Analyze structured data, generate insights, build dashboards, run statistical evaluations.

**Responsibilities:**
- Query SQLite task database and PostgreSQL audit logs
- Generate usage/performance reports
- Analyze model routing efficiency and cost
- Build metric summaries for dashboard
- Evaluate agent performance over time

**Inputs:** Analysis request, date range, metric focus.

**Outputs:** Structured report, chart data (JSON for frontend), SQL queries used.

**Allowed tools:**
```
read_file, run_sql (read-only), web_search, memory_lookup
```

**Restricted tools:**
```
write_file (code), run_shell, git_*, send_*, write_sql (DDL/DML)
```

**Memory access:** `project` (read), `audit_log` (read), `knowledge/analytics` (read/write)

**Review requirements:** Reports that will drive major product decisions reviewed by Manager.

---

### 3.10 automation_engineer

**Purpose:** Design and implement recurring workflow automations, cron jobs, and event-driven pipelines.

**Responsibilities:**
- Build Redis Streams consumer workflows
- Implement scheduled task definitions
- Wire new event types into the event bus
- Build connector integrations (Slack, GitHub webhooks, etc.)
- Monitor automation health

**Inputs:** Automation spec, trigger definition, output contract.

**Outputs:** Python workflow module, cron config, event schema, integration test.

**Allowed tools:**
```
read_file, write_file, run_shell (restricted), run_tests, web_search
```

**Restricted tools:**
```
git_push, deploy, send_* (without approval), delete_*
```

**Memory access:** `project` (read/write), `knowledge/automation` (read/write)

**Review requirements:** Any automation that sends external messages requires Security Reviewer + human approval.

---

### 3.11 career_agent

**Purpose:** Job search, resume tailoring, cover letter generation, interview preparation, application tracking.

**Responsibilities:**
- Score job postings against Aman's profile
- Tailor resume for specific roles
- Generate targeted cover letters
- Build role-specific interview prep stories
- Track application pipeline status
- Research target companies

**Inputs:** Job posting URL or description, target role, Aman's current resume/profile.

**Outputs:** Job score report, tailored resume diff, cover letter, interview prep document.

**Allowed tools:**
```
web_search, web_fetch, read_file, vault_write, memory_lookup
```

**Restricted tools:**
```
send_email (requires explicit approval), run_shell, git_*
```

**Memory access:** `personal/*` (read/write), `knowledge/career` (read/write), `project` (read)

**Review requirements:** Resume changes require human approval before use. Application submissions require explicit human confirmation.

**Failure handling:** Never submit applications without explicit approval. If job score < 40, flag as poor fit before proceeding.

---

### 3.12 ai_safety_agent

**Purpose:** Policy analysis, harm classification, incident review, threat intelligence for AI safety operations.

**Responsibilities:**
- Classify content against harm taxonomy
- Analyze policy documents for gaps
- Review model outputs for safety failures
- Generate safety incident reports
- Track regulatory developments (EU AI Act, NIST, etc.)
- Evaluate Jarvis's own outputs for safety compliance

**Inputs:** Content to classify, policy document, incident description, or monitoring query.

**Outputs:** Classification result (harm type + severity), policy gap analysis, incident report (CVSS-style scoring).

**Allowed tools:**
```
web_search, read_file, vault_write, memory_lookup
```

**Restricted tools:**
```
write_file (code), run_shell, send_*, git_*
```

**Memory access:** `knowledge/ai_safety` (read/write), `audit_log` (read/write), `project` (read)

**Review requirements:** CRITICAL harm classifications escalate to human immediately. Policy recommendations reviewed by Manager.

---

### 3.13 gsoc_agent

**Purpose:** GSOC (trust & safety) operations: incident triage, escalation management, investigation support, reporting.

**Responsibilities:**
- Triage incoming safety incidents by severity
- Draft escalation communications
- Investigate patterns across incident history
- Generate weekly/monthly operational reports
- Track SLA compliance

**Inputs:** Incident description or ID, investigation scope, reporting period.

**Outputs:** Triage decision (P0/P1/P2/P3), escalation draft, investigation summary, SLA report.

**Allowed tools:**
```
web_search, read_file, vault_write, memory_lookup, run_sql (read-only)
```

**Restricted tools:**
```
send_message (draft only, requires approval), write_file (code), run_shell
```

**Memory access:** `knowledge/gsoc` (read/write), `audit_log` (read), `personal/restricted` (none)

**Review requirements:** P0/P1 incidents: human approval before any external communication. Escalation drafts are always drafts — never auto-sent.

---

## 4. Security Model

### 4.1 RBAC Permission Levels

```
HUMAN        All permissions. Approves sensitive actions. Cannot be impersonated.
MANAGER      All agent permissions + approval_gate_control + memory_admin.
AGENT        Tool set defined per agent. Cannot elevate own permissions.
READONLY     read_file, memory_lookup, web_search only. No writes.
```

### 4.2 Permission Matrix

```
Action                          HUMAN  MANAGER  AGENT  READONLY
─────────────────────────────────────────────────────────────────
read_file                         ✓      ✓        ✓       ✓
write_file                        ✓      ✓      scoped    ✗
run_shell                         ✓      ✓      scoped    ✗
git_push                          ✓      ✗        ✗       ✗
send_email                        ✓      gate     gate    ✗
send_message                      ✓      gate     gate    ✗
deploy_production                 ✓      ✗        ✗       ✗
memory_write                      ✓      ✓      scoped    ✗
memory_admin (delete/prune)       ✓      ✓        ✗       ✗
audit_log_read                    ✓      ✓        ✗       ✗
rbac_admin                        ✓      ✗        ✗       ✗
approval_gate_control             ✓      ✓        ✗       ✗
```

`gate` = requires approval_gate before execution.

### 4.3 Threat Models

**Prompt Injection**
- Attack: Malicious content in web search results, file reads, or user messages overrides agent instructions.
- Mitigation: All external content wrapped in `<external_content>` XML tags before injection into prompts. System prompt always comes first in the message array and is never user-modifiable. Agent system prompts are immutable at runtime.

**Tool Abuse**
- Attack: Agent calls tools outside its allowed set, or chains tool calls to escalate privileges.
- Mitigation: Tool calls validated against per-agent allowlist before execution. Tool executor is separate from agent runner — agent cannot call executor directly. All tool calls logged to audit trail.

**Memory Poisoning**
- Attack: Agent writes false facts to memory that corrupt future reasoning.
- Mitigation: Memory writes from agents pass through `memory_librarian` validation. Confidence threshold required for fact writes. Human approval for memory_admin operations. Memory layers are append-only with soft deletes.

**Agent Impersonation**
- Attack: One agent claims to be another agent or the Manager.
- Mitigation: Each agent session gets a signed JWT with `agent_id` and `permission_level` claims. All inter-agent messages include signed headers. Agent cannot modify its own JWT.

**Unauthorized Code Execution**
- Attack: LLM output causes execution of attacker-controlled code.
- Mitigation: `run_shell` restricted to agents with explicit grant. Commands validated against allowlist patterns before execution. No `shell=True`. Subprocess timeout enforced. Output sanitized before returning to LLM.

**Data Leakage**
- Attack: Agent exfiltrates sensitive data (API keys, personal info) via web calls or logs.
- Mitigation: `~/.env` and secrets never included in file reads. Web calls require `web_search` or `web_fetch` tool grant. Outbound HTTP from agents goes through `@openclaw/proxyline` (loopback proxy with URL allowlist). Logs scrubbed of secret patterns before write.

### 4.4 Secrets Management

Secrets live exclusively in `~/.env`. No agent receives raw secret values.

```python
# jarvis_os/secrets.py
import os, re
from pathlib import Path

_SECRET_PATTERNS = re.compile(
    r'(OPENAI|ANTHROPIC|ELEVENLABS|GEMINI|GOOGLE|API_KEY|SECRET|TOKEN|PASSWORD)',
    re.I
)

def get_secret(name: str) -> str:
    """Return secret by name. Agents call this — never read .env directly."""
    val = os.getenv(name, "")
    if not val:
        raise KeyError(f"Secret {name} not set")
    return val

def scrub_for_log(text: str) -> str:
    """Remove any secret-shaped strings before writing to audit log."""
    lines = text.splitlines()
    clean = []
    for line in lines:
        if _SECRET_PATTERNS.search(line) and '=' in line:
            key, _ = line.split('=', 1)
            clean.append(f"{key}=[REDACTED]")
        else:
            clean.append(line)
    return '\n'.join(clean)
```

---

## 5. Memory Architecture

### 5.1 Memory Layers

```
Layer 0 — Working Memory (in-process)
  conversation_context.py    Active conversation turns (last 8)
  TTL: process lifetime      No persistence

Layer 1 — Project Memory (SQLite + JSON)
  task_persistence.py        Task history, agent outputs, diffs
  semantic_memory.py         TF-IDF indexed facts per project
  TTL: 90 days               Pruned by memory_librarian

Layer 2 — Personal Memory (PostgreSQL + Qdrant)
  memory_pg.py               Structured facts about Aman
  mem0_layer.py              Cross-session episodic memory
  semantic_memory.py         Career/interview stories
  TTL: indefinite            Curated by memory_librarian

Layer 3 — Knowledge Memory (Vault + Qdrant)
  vault.py                   Obsidian markdown files
  nomic-embed-text           Dense vector embeddings
  semantic_memory.py         TF-IDF keyword index
  TTL: indefinite            Append-only with explicit deprecation
```

### 5.2 PostgreSQL Schema

```sql
-- Core memory schema
-- Run: psql jarvis < docs/schema.sql

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- for fuzzy text search

-- ── Memory entries ────────────────────────────────────────────────────────────

CREATE TABLE memory_entries (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    layer         TEXT NOT NULL CHECK (layer IN ('project','personal','knowledge')),
    category      TEXT NOT NULL,  -- e.g. 'career', 'ai_safety', 'engineering'
    content       TEXT NOT NULL,
    embedding_id  TEXT,           -- Qdrant point ID
    confidence    FLOAT NOT NULL DEFAULT 1.0,
    source_agent  TEXT NOT NULL,
    source_task   TEXT,           -- task_id that created this
    tags          TEXT[] NOT NULL DEFAULT '{}',
    metadata      JSONB NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at    TIMESTAMPTZ,    -- soft delete
    version       INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX idx_memory_layer_category ON memory_entries(layer, category) WHERE deleted_at IS NULL;
CREATE INDEX idx_memory_tags ON memory_entries USING GIN(tags);
CREATE INDEX idx_memory_metadata ON memory_entries USING GIN(metadata);
CREATE INDEX idx_memory_content_trgm ON memory_entries USING GIN(content gin_trgm_ops);
CREATE INDEX idx_memory_created ON memory_entries(created_at DESC);

-- ── Audit log ────────────────────────────────────────────────────────────────

CREATE TABLE audit_log (
    id            BIGSERIAL PRIMARY KEY,
    event_type    TEXT NOT NULL,  -- 'tool_call', 'approval', 'memory_write', 'auth'
    agent_id      TEXT NOT NULL,
    task_id       TEXT,
    tool_name     TEXT,
    action        TEXT NOT NULL,
    outcome       TEXT NOT NULL CHECK (outcome IN ('success','failure','blocked','pending')),
    risk_level    TEXT NOT NULL DEFAULT 'low' CHECK (risk_level IN ('critical','high','medium','low','info')),
    details       JSONB NOT NULL DEFAULT '{}',
    ip_addr       INET,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_agent ON audit_log(agent_id, created_at DESC);
CREATE INDEX idx_audit_event ON audit_log(event_type, created_at DESC);
CREATE INDEX idx_audit_risk ON audit_log(risk_level, created_at DESC) WHERE risk_level IN ('critical','high');

-- ── RBAC ─────────────────────────────────────────────────────────────────────

CREATE TABLE agent_permissions (
    agent_id        TEXT PRIMARY KEY,
    permission_level TEXT NOT NULL CHECK (permission_level IN ('human','manager','agent','readonly')),
    allowed_tools   TEXT[] NOT NULL DEFAULT '{}',
    memory_scopes   TEXT[] NOT NULL DEFAULT '{}',  -- e.g. ['project:read','personal:write']
    approval_gates  TEXT[] NOT NULL DEFAULT '{}',  -- actions requiring approval
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed agent permissions
INSERT INTO agent_permissions (agent_id, permission_level, allowed_tools, memory_scopes, approval_gates) VALUES
('backend_engineer',  'agent',    ARRAY['read_file','write_file','run_shell','web_search','run_tests','git_diff'], ARRAY['project:rw','knowledge/engineering:r'], ARRAY['git_push','send_email','deploy']),
('frontend_designer', 'agent',    ARRAY['read_file','write_file','web_search'],                                   ARRAY['project:r','knowledge/design:rw'],      ARRAY['git_push','deploy']),
('security_reviewer', 'agent',    ARRAY['read_file','web_search','run_shell'],                                    ARRAY['knowledge/security:rw','audit:w'],      ARRAY['*']),
('qa_tester',         'agent',    ARRAY['read_file','write_file','run_tests','run_shell'],                        ARRAY['project:r','knowledge/testing:rw'],     ARRAY['git_push','deploy']),
('researcher',        'agent',    ARRAY['web_search','web_fetch','read_file','vault_write'],                      ARRAY['knowledge:rw','project:r'],             ARRAY['send_email']),
('memory_librarian',  'agent',    ARRAY['read_file','vault_write','vault_read','memory_write'],                   ARRAY['all:rw'],                               ARRAY['memory_delete']),
('career_agent',      'agent',    ARRAY['web_search','web_fetch','read_file','vault_write'],                      ARRAY['personal:rw','knowledge/career:rw'],    ARRAY['send_email','send_message']),
('ai_safety_agent',   'agent',    ARRAY['web_search','read_file','vault_write'],                                  ARRAY['knowledge/ai_safety:rw','audit:w'],     ARRAY['send_message']),
('gsoc_agent',        'agent',    ARRAY['web_search','read_file','vault_write','run_sql'],                        ARRAY['knowledge/gsoc:rw','audit:r'],          ARRAY['send_message','send_email']),
('devops_release',    'agent',    ARRAY['read_file','write_file','run_shell','run_tests','git_status'],           ARRAY['project:rw','knowledge/infra:rw'],      ARRAY['git_push','deploy','delete_volume']),
('data_analyst',      'readonly', ARRAY['read_file','run_sql','web_search'],                                      ARRAY['project:r','audit:r'],                  ARRAY['*']),
('ux_researcher',     'readonly', ARRAY['web_search','read_file','vault_write'],                                  ARRAY['knowledge/ux:rw','project:r'],          ARRAY['*']),
('automation_engineer','agent',   ARRAY['read_file','write_file','run_shell','run_tests'],                        ARRAY['project:rw','knowledge/automation:rw'], ARRAY['git_push','send_*','deploy']);

-- ── Review chains ────────────────────────────────────────────────────────────

CREATE TABLE review_chains (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    task_id         TEXT NOT NULL,
    chain_name      TEXT NOT NULL,  -- 'code_review', 'security_review', 'deployment'
    stages          JSONB NOT NULL, -- ordered array of {reviewer, status, verdict, notes}
    current_stage   INTEGER NOT NULL DEFAULT 0,
    final_status    TEXT CHECK (final_status IN ('pending','approved','rejected','bypassed')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX idx_review_task ON review_chains(task_id);
CREATE INDEX idx_review_status ON review_chains(final_status) WHERE final_status = 'pending';
```

### 5.3 Retrieval Strategy

```python
# Priority order for memory retrieval (jarvis_os/memory_retrieval.py)
#
# 1. Working memory (in-process, < 1ms)
#    conversation_context.get_recent_turns(n=8)
#
# 2. Semantic search — Qdrant dense vectors (< 50ms)
#    nomic-embed-text embeddings, cosine similarity, top-k=5
#    Use for: personal facts, career stories, domain knowledge
#
# 3. TF-IDF keyword index (< 10ms, fallback when Qdrant cold)
#    semantic_memory.retrieve(query, top_k=3)
#    Use for: project-specific terms, code symbols, proper nouns
#
# 4. Vault full-text search (rg, < 100ms)
#    vault.search(query, topn=3)
#    Use for: long-form knowledge, research reports, notes
#
# 5. PostgreSQL JSONB query (< 200ms)
#    memory_entries WHERE tags && ARRAY[...] AND layer = '...'
#    Use for: structured facts, tagged personal data

RETRIEVAL_BUDGET_MS = 300  # max total retrieval time per request
```

### 5.4 Memory Pruning Policy

```
Working memory:      Evict after 8 turns. Non-negotiable.
Project memory:      Prune tasks older than 90 days with status 'succeeded'.
                     Keep ALL failed tasks (learning signal).
                     Keep tasks referenced by memory entries.
Personal memory:     Never auto-prune. Human approval required for deletion.
Knowledge memory:    Deprecate (don't delete) entries superseded by newer content.
                     Deprecated entries retain 30-day grace period before archival.
```

---

## 6. Event Bus Architecture

### 6.1 Redis Streams Schema

```
Stream: jarvis:tasks             Task lifecycle events
Stream: jarvis:agent:results     Agent output events
Stream: jarvis:review            Review chain events
Stream: jarvis:memory            Memory write events
Stream: jarvis:audit             Security/audit events (append-only)
Stream: jarvis:alerts            Proactive alerts for human

Consumer Groups:
  jarvis:tasks → group:manager      (Jarvis Manager consumes)
  jarvis:tasks → group:agents       (Agent workers consume)
  jarvis:review → group:reviewers   (Review chain processors)
```

### 6.2 Event Envelope

```python
# All events follow this envelope (jarvis_os/events.py)
@dataclass
class JarvisEvent:
    event_id: str          # ULID
    event_type: str        # e.g. 'task.created', 'agent.completed', 'approval.requested'
    source_agent: str      # who emitted
    task_id: str | None
    payload: dict          # event-specific data
    priority: int          # 0=critical, 1=high, 2=normal, 3=low
    created_at: str        # ISO8601
    ttl_seconds: int       # 0 = no expiry

# Event types
TASK_CREATED           = "task.created"
TASK_ASSIGNED          = "task.assigned"
TASK_STARTED           = "task.started"
TASK_COMPLETED         = "task.completed"
TASK_FAILED            = "task.failed"
APPROVAL_REQUESTED     = "approval.requested"
APPROVAL_GRANTED       = "approval.granted"
APPROVAL_DENIED        = "approval.denied"
REVIEW_STARTED         = "review.started"
REVIEW_COMPLETED       = "review.completed"
MEMORY_WRITE           = "memory.write"
SECURITY_FINDING       = "security.finding"
AGENT_HEARTBEAT        = "agent.heartbeat"
```

---

## 7. Review Chain Engine

### 7.1 Review Workflow Definitions

```python
# jarvis_os/review_chains.py

REVIEW_WORKFLOWS = {
    "code": [
        {"reviewer": "qa_tester",        "required": True,  "timeout_minutes": 30},
        {"reviewer": "security_reviewer", "required": True,  "timeout_minutes": 20},
        {"reviewer": "jarvis_manager",   "required": True,  "timeout_minutes": 5},
        {"reviewer": "human",            "required": False, "condition": "has_breaking_change"},
    ],
    "security_finding": [
        {"reviewer": "jarvis_manager",   "required": True,  "timeout_minutes": 5},
        {"reviewer": "human",            "required": True,  "condition": "severity in (CRITICAL, HIGH)"},
    ],
    "deployment": [
        {"reviewer": "qa_tester",        "required": True,  "timeout_minutes": 60},
        {"reviewer": "security_reviewer", "required": True,  "timeout_minutes": 30},
        {"reviewer": "human",            "required": True,  "timeout_minutes": None},  # no timeout
    ],
    "resume_update": [
        {"reviewer": "career_agent",     "required": True,  "timeout_minutes": 10},
        {"reviewer": "human",            "required": True,  "timeout_minutes": None},
    ],
    "job_application": [
        {"reviewer": "career_agent",     "required": True,  "timeout_minutes": 10},
        {"reviewer": "human",            "required": True,  "timeout_minutes": None},
    ],
    "research_report": [
        {"reviewer": "jarvis_manager",   "required": True,  "timeout_minutes": 15},
    ],
    "ai_safety_report": [
        {"reviewer": "ai_safety_agent",  "required": True,  "timeout_minutes": 20},
        {"reviewer": "human",            "required": True,  "condition": "severity >= HIGH"},
    ],
}
```

---

## 8. Model Routing Matrix

```
Query Type              Local First          Cloud Escalation Condition
─────────────────────────────────────────────────────────────────────────
Fast chat               glm-4.7-flash        Never (local is sufficient)
Code generation         glm-4.7-flash        Token count > 80k
Code review             glm-4.7-flash        Multi-file cross-repo analysis
Research synthesis      qwen3:8b             Source count > 20
Planning (complex)      qwen3:8b             Multi-week, multi-agent plans
Security review         glm-4.7-flash        CRITICAL findings need 2nd opinion
Summarization           glm-4.7-flash        Never
Memory retrieval        nomic-embed-text      Never (embedding is local)
Vision analysis         llava:7b             Never
Structured output       glm-4.7-flash        Schema complexity > 20 fields
Intent classification   glm-4.7-flash        Never
Long context (>80k)     glm-4.7-flash*       * uses 202k context natively
```

**Model assignment by agent:**
```
backend_engineer:   glm-4.7-flash → qwen3:8b (complex multi-file refactors)
frontend_designer:  glm-4.7-flash
ux_researcher:      glm-4.7-flash
security_reviewer:  glm-4.7-flash (paranoid mode: no cloud for security review)
qa_tester:          glm-4.7-flash
researcher:         qwen3:8b (synthesis) + nomic-embed-text (retrieval)
devops_release:     glm-4.7-flash
memory_librarian:   nomic-embed-text (indexing) + glm-4.7-flash (curation)
data_analyst:       glm-4.7-flash
automation_engineer: glm-4.7-flash
career_agent:       qwen3:8b (resume writing needs quality)
ai_safety_agent:    qwen3:8b (nuanced policy analysis)
gsoc_agent:         glm-4.7-flash
```

---

## 9. Production Folder Structure

```
jarvis-ai/
├── jarvis_os/                  ← NEW: core OS infrastructure
│   ├── __init__.py
│   ├── event_bus.py            Redis Streams event bus
│   ├── rbac.py                 Permission enforcement
│   ├── review_chain.py         Review workflow engine
│   ├── agent_sandbox.py        Agent subprocess isolation
│   ├── secrets.py              Secret management
│   ├── memory_pg.py            PostgreSQL memory backend
│   ├── audit.py                Audit log writer
│   └── events.py               Event type definitions
│
├── brains/                     ← EXISTING: model adapters
│   ├── brain_ollama.py         Ollama + tool calling loop (updated)
│   ├── brain_claude.py
│   ├── brain_gemini.py
│   └── brain.py
│
├── agents/                     ← EXISTING: agent instruction files
│   ├── backend_engineer.md     (expand with full spec)
│   ├── security_reviewer.md
│   └── ...
│
├── agent_dispatch.py           ← EXISTING: tool-calling dispatch
├── task_runtime.py             ← EXISTING: task lifecycle
├── task_persistence.py         ← EXISTING: SQLite persistence
├── orchestrator.py             ← EXISTING: intent classification
├── router.py                   ← EXISTING: request routing
├── model_router.py             ← EXISTING: model selection
│
├── workflows/                  ← NEW: automation workflow definitions
│   ├── software_engineering.py
│   ├── career_ops.py
│   ├── ai_safety_ops.py
│   ├── gsoc_ops.py
│   └── research_ops.py
│
├── docker/                     ← NEW: container definitions
│   ├── docker-compose.yml
│   ├── postgres/
│   │   └── init.sql
│   ├── redis/
│   │   └── redis.conf
│   └── monitoring/
│       ├── prometheus.yml
│       └── grafana/
│
├── docs/
│   ├── ARCHITECTURE.md         (this file)
│   ├── SECURITY.md
│   ├── ROADMAP.md
│   └── schema.sql
│
└── tests/
    ├── test_agent_dispatch.py  ← EXISTING
    ├── test_rbac.py            ← NEW
    ├── test_review_chains.py   ← NEW
    └── test_event_bus.py       ← NEW
```

---

## 10. Docker Architecture

```yaml
# docker/docker-compose.yml
version: "3.9"

services:
  jarvis:
    build: .
    ports:
      - "8765:8765"
    environment:
      - REDIS_URL=redis://redis:6379
      - POSTGRES_URL=postgresql://jarvis:${POSTGRES_PASSWORD}@postgres:5432/jarvis
      - QDRANT_URL=http://qdrant:6333
      - OLLAMA_HOST=http://host.docker.internal:11434  # Ollama runs on host GPU
    volumes:
      - ~/.env:/app/.env:ro
      - jarvis_data:/app/runtime
      - ~/jarvis-ai/vault:/app/vault
    depends_on: [redis, postgres, qdrant]
    restart: unless-stopped

  redis:
    image: redis:7.2-alpine
    command: redis-server /etc/redis/redis.conf
    volumes:
      - ./redis/redis.conf:/etc/redis/redis.conf:ro
      - redis_data:/data
    ports:
      - "127.0.0.1:6379:6379"  # loopback only
    restart: unless-stopped

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: jarvis
      POSTGRES_USER: jarvis
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes:
      - postgres_data:/var/lib/postgresql/data
      - ./postgres/init.sql:/docker-entrypoint-initdb.d/init.sql:ro
    ports:
      - "127.0.0.1:5432:5432"  # loopback only
    restart: unless-stopped

  qdrant:
    image: qdrant/qdrant:latest
    volumes:
      - qdrant_data:/qdrant/storage
    ports:
      - "127.0.0.1:6333:6333"  # loopback only
    restart: unless-stopped

  prometheus:
    image: prom/prometheus:latest
    volumes:
      - ./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml:ro
    ports:
      - "127.0.0.1:9090:9090"
    restart: unless-stopped

  grafana:
    image: grafana/grafana:latest
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_PASSWORD}
    volumes:
      - grafana_data:/var/lib/grafana
      - ./monitoring/grafana:/etc/grafana/provisioning:ro
    ports:
      - "127.0.0.1:3000:3000"
    restart: unless-stopped

volumes:
  jarvis_data:
  redis_data:
  postgres_data:
  qdrant_data:
  grafana_data:
```

---

## 11. API Contracts

### Core endpoints (existing)
```
POST /chat                    Main conversation endpoint (SSE streaming)
GET  /status                  Runtime health
GET  /agents                  List dispatch agents (name/role/model)
POST /agents/{name}/run       Run agent task (SSE streaming)
GET  /tasks                   List tasks
POST /tasks                   Submit task
GET  /tasks/{id}              Get task
POST /tasks/{id}/approve      Approve waiting task
POST /tasks/{id}/cancel       Cancel task
GET  /v1/models               OpenAI-compat model list (for OpenClaw)
POST /v1/chat/completions     OpenAI-compat chat endpoint (for OpenClaw)
```

### New endpoints (to build)
```
POST /agents/{name}/run-reviewed  Run agent with review chain, returns review_chain_id
GET  /review/{id}                 Get review chain state
POST /review/{id}/approve         Human approves a review stage
POST /review/{id}/reject          Human rejects with reason

GET  /memory/search               Semantic search across all layers
POST /memory/write                Write to memory (manager/human only)
DELETE /memory/{id}               Soft-delete (human only, creates audit entry)

GET  /audit                       Audit log (paginated, filtered)
GET  /audit/summary               Aggregate risk summary

GET  /workflows                   List available workflow definitions
POST /workflows/{name}/run        Trigger named workflow
GET  /workflows/{run_id}/status   Workflow run status

GET  /metrics                     Prometheus metrics endpoint
GET  /health                      Liveness check (returns 200 if Ollama + DB up)
```

### Request/Response patterns
All endpoints follow:
```json
{
  "ok": true,
  "data": { ... },
  "error": null,
  "request_id": "ulid"
}
```

Errors:
```json
{
  "ok": false,
  "data": null,
  "error": { "code": "permission_denied", "message": "...", "details": {} },
  "request_id": "ulid"
}
```
