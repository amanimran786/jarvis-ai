# Jarvis Adoption Plan: MemPalace + Graphify

This document translates two external projects into concrete Jarvis upgrades:

- `milla-jovovich/mempalace`
- `safishamsi/graphify`

Goal: improve Jarvis memory quality and repo-grounded reasoning while staying local-first.

---

## 1) What We Can Add From MemPalace

### A. Verbatim Conversation Archive (High Value)

MemPalace's strongest idea is simple: keep full conversations verbatim, then retrieve precisely.

Jarvis action:

- add a new persistent conversation corpus under `memory/conversations/`
- store full turn history (not only summaries)
- index with local embeddings and metadata filters

Target modules:

- [memory.py](/Users/truthseeker/jarvis-ai/memory.py)
- [semantic_memory.py](/Users/truthseeker/jarvis-ai/semantic_memory.py)
- [conversation_context.py](/Users/truthseeker/jarvis-ai/conversation_context.py)

Why:

- better recall for "why we decided X"
- less memory drift from over-summarization
- stronger personalization without cloud dependency

### B. Memory Zones (Medium Value)

MemPalace uses structure (wings/rooms/halls). Jarvis can adopt a practical equivalent:

- `person/*` (user profile)
- `project/*` (repo + product decisions)
- `ops/*` (runtime incidents, regressions, fixes)
- `meeting/*` (live call context)

Target modules:

- [semantic_memory.py](/Users/truthseeker/jarvis-ai/semantic_memory.py)
- [vault.py](/Users/truthseeker/jarvis-ai/vault.py)

Why:

- faster retrieval
- cleaner trust/privacy boundaries
- better filtering for the right context

### C. Wake-Up Context Pack (Medium Value)

MemPalace's wake-up concept is useful: preload a tiny, high-signal memory pack before answering.

Jarvis action:

- generate a compact "startup context block" from local memory
- include current goals, active project state, and recent critical decisions
- inject it into early-session routing only

Target modules:

- [model_router.py](/Users/truthseeker/jarvis-ai/model_router.py)
- [runtime_state.py](/Users/truthseeker/jarvis-ai/runtime_state.py)

---

## 2) What We Can Add From Graphify

### A. Evidence-Graded Graph Edges (High Value)

Graphify marks relationships as extracted vs inferred. Jarvis should do the same in graph context.

Jarvis action:

- extend graph artifacts with edge provenance tags:
  - `EXTRACTED`
  - `INFERRED`
  - `AMBIGUOUS`
- bias retrieval toward `EXTRACTED` first

Target modules:

- [scripts/build_graphify_repo.py](/Users/truthseeker/jarvis-ai/scripts/build_graphify_repo.py)
- [graph_context.py](/Users/truthseeker/jarvis-ai/graph_context.py)

Why:

- more trustworthy architecture answers
- clearer "found vs guessed" boundaries

### B. Graph Query/Path Explain Endpoints (High Value)

Graphify's `query/path/explain` pattern maps well to Jarvis API/CLI.

Jarvis action:

- add API endpoints:
  - `GET /graph/query?q=...`
  - `GET /graph/path?from=...&to=...`
  - `GET /graph/explain?node=...`
- add CLI wrappers:
  - `jarvis_cli.py --graph-query "..."`
  - `jarvis_cli.py --graph-path A B`

Target modules:

- [api.py](/Users/truthseeker/jarvis-ai/api.py)
- [jarvis_cli.py](/Users/truthseeker/jarvis-ai/jarvis_cli.py)
- [graph_context.py](/Users/truthseeker/jarvis-ai/graph_context.py)

Why:

- faster root-cause and architecture reasoning
- less raw file searching

### C. Incremental Graph Refresh Hooks (Medium Value)

Graphify's incremental refresh is practical for Jarvis repo grounding freshness.

Jarvis action:

- add optional git hook installer to refresh graph on commit/checkout
- rebuild only changed files via content hash cache

Target modules:

- [scripts/build_graphify_repo.py](/Users/truthseeker/jarvis-ai/scripts/build_graphify_repo.py)
- [scripts/](/Users/truthseeker/jarvis-ai/scripts)

---

## 3) Proposed Build Order

1. Untrack and ignore runtime logs (security hygiene)
2. Verbatim conversation archive in semantic memory
3. Graph query/path/explain API and CLI
4. Edge provenance tags in graph artifacts
5. Wake-up context pack generation
6. Incremental graph refresh hooks

---

## 4) Risks and Boundaries

- Verbatim memory can increase local storage; add retention controls.
- Inferred edges must be clearly marked to avoid overconfident answers.
- Hook-based graph refresh should be opt-in to avoid slowing developer workflows.
- Keep all additions local-first and deterministic by default.

---

## 5) Immediate Next Implementation Slice

If we start now, the highest-leverage concrete slice is:

- implement verbatim conversation indexing in [semantic_memory.py](/Users/truthseeker/jarvis-ai/semantic_memory.py)
- add `graph query/path` endpoints in [api.py](/Users/truthseeker/jarvis-ai/api.py)
- add matching CLI commands in [jarvis_cli.py](/Users/truthseeker/jarvis-ai/jarvis_cli.py)

This gives direct user-visible quality gains without changing the UI first.
