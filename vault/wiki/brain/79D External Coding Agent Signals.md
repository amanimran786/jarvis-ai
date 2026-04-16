---
type: investigation
area: engineering
owner: jarvis
write_policy: curated
review_required: false
status: active
source: github
confidence: medium
created: 2026-04-16
updated: 2026-04-16
version: 1
tags:
  - github
  - coding-agents
  - obsidian
  - memory
related:
  - "[[79 Coding Implementation Playbook]]"
  - "[[79B Jarvis Architecture Runtime Seams]]"
  - "[[80 Jarvis Roadmap]]"
---

# External Coding Agent Signals

Purpose: keep a durable record of which public repos are worth borrowing from for Jarvis’s coding intelligence.

Linked notes: [[79 Coding Implementation Playbook]], [[79B Jarvis Architecture Runtime Seams]], [[80 Jarvis Roadmap]]

## Coding-Agent Repos Worth Learning From

- `sst/opencode` (`144k` stars, checked 2026-04-16)
  - strongest signal: dedicated open coding-agent runtime with terminal-first ergonomics
  - import into Jarvis: keep a first-class coding role instead of routing every implementation request through generic planner/executor logic

- `All-Hands-AI/OpenHands` (`71k` stars, checked 2026-04-16)
  - strongest signal: separate SDK, CLI, and local GUI surfaces with benchmark discipline
  - import into Jarvis: treat coding competence as a measurable runtime capability, not just prompt wording

- `cline/cline` (`60k` stars, checked 2026-04-16)
  - strongest signal: permissioned tool use and explicit user-visible action boundaries
  - import into Jarvis: keep bounded native/operator actions and do not hide risky writes behind agent autonomy

- `Aider-AI/aider` (`43k` stars, checked 2026-04-16)
  - strongest signal: repo map, git-aware edits, and automatic lint/test loop
  - import into Jarvis: preserve repo-grounded coding, smallest diffs, and narrow post-edit verification

## Memory and Obsidian Repos Worth Learning From

- `mem0ai/mem0` (`53k` stars, checked 2026-04-16)
  - strongest signal: explicit memory layers
  - import into Jarvis: keep user, session, and agent memory contracts explicit
  - do not import directly: defaults assume external services that conflict with Jarvis local-first goals

- `brianpetro/obsidian-smart-connections` (`4.8k` stars, checked 2026-04-16)
  - strongest signal: local embeddings and semantic resurfacing inside the vault
  - import into Jarvis: keep local semantic recall and related-note clustering strong inside the Obsidian brain

- `coddingtonbear/obsidian-local-rest-api` (`2.0k` stars, checked 2026-04-16)
  - strongest signal: surgical heading/frontmatter patch API
  - import into Jarvis: continue building heading-level vault mutation instead of whole-file rewrites

- `iansinnott/obsidian-claude-code-mcp` (`245` stars, checked 2026-04-16)
  - strongest signal: MCP bridge between coding agents and Obsidian vaults
  - import into Jarvis: if we expose the vault externally later, do it through a thin local protocol surface, not direct uncontrolled file mutation

## Curation Repo Worth Watching

- `hesreallyhim/awesome-claude-code` (`39k` stars, checked 2026-04-16)
  - strongest signal: useful pattern inventory, not a runtime dependency
  - import into Jarvis: review selectively for skills, hooks, and agent patterns, but keep Jarvis opinionated and repo-specific

## Current Decision

Borrow patterns, not platforms.

The right imports for Jarvis are:
- dedicated coding role
- repo-grounded coding playbooks
- explicit verification loops
- layered memory contracts
- thin local vault bridges

The wrong imports are:
- cloud-first orchestration
- generic multi-agent theater
- direct adoption of external memory backends that bypass the local brain
