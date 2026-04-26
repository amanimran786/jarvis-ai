---
type: brain_note
area: jarvis
owner: jarvis
write_policy: append_only
review_required: false
status: active
source: web_research
confidence: high
created: 2026-04-26
updated: 2026-04-26
version: 1
tags:
  - jarvis
  - cli
  - agents
  - skills
  - local-first
related:
  - "[[78 AI Runtime Agent Engineering Principles]]"
  - "[[79 Local Skill Loop]]"
  - "[[82 Context Budget Discipline]]"
  - "[[83 External Agent Pattern Intake]]"
  - "[[88 Coder Workbench]]"
---

# CLI Agent Advances

Purpose: track useful CLI-agent patterns Jarvis should adapt without losing the local-first macOS product contract.

Linked notes: [[78 AI Runtime Agent Engineering Principles]], [[79 Local Skill Loop]], [[82 Context Budget Discipline]], [[83 External Agent Pattern Intake]], [[88 Coder Workbench]]

## Current Signals

### Portable agent skills

GitHub launched `gh skill` on 2026-04-16 for discovering, installing, managing, updating, previewing, and publishing agent skills from GitHub repositories. The strongest Jarvis lesson is not blind installation. The lesson is portable provenance: skills need source repo, pinned ref, tree SHA, preview, update, and dry-run validation metadata.

Jarvis path: add a guarded `.agents/skills` compatibility exporter/importer that maps Jarvis `skills/` into the shared project-scope skill layout used by Codex, Gemini CLI, OpenCode, Cursor, and other hosts, while keeping `skills/index.json`, `AGENTS.md`, and `CLAUDE.md` canonical.

Adopt: provenance metadata, preview-before-install, pinning, update checks.

Reject: installer-driven overwrites of Jarvis canonical instructions or executable scripts without review.

### Attachable long-running backends

OpenCode documents a split between a long-running backend and attachable TUI clients. This matches Jarvis's FastAPI daemon and terminal console architecture.

Jarvis path: strengthen `jarvis_cli.py` as a thin attachable client:

- `/sessions` to list recent console/task sessions
- `/resume <id>` to reopen a session context
- `/fork <id>` to branch a task/session safely
- `/attach <url>` only for local/trusted endpoints by default

Adopt: attach/resume/fork semantics, programmatic command surface, backend-client split.

Reject: remote listen on `0.0.0.0` without an explicit auth and network exposure gate.

### Context and permission systems

Recent Claude Code architecture analysis highlights that the agent loop is simple, but the surrounding system matters: permission modes, context compaction, skills/plugins/hooks, subagent worktree isolation, and append-oriented session storage.

Jarvis path: Jarvis already has `permissions`, `context-budget`, `skills`, `plugins`, `tasks`, and `coder_workbench`. The missing CLI seam is a concise "mission control" view that shows:

- current daemon identity
- active task IDs
- pending approvals
- token/context budget
- dirty git files
- live packaged-app identity

Adopt: mission-control status, append-only session logs, explicit approval queue.

Reject: hidden auto-compaction that drops user-visible evidence.

### Skill package quality checks

Skilldex proposes compiler-style diagnostics for skill packages and bundled skillsets with shared assets. Jarvis already has negative triggers and proposal-first skill creation.

Jarvis path: add `jarvis --skill-audit` to validate:

- frontmatter fields
- trigger specificity
- negative triggers
- source/provenance
- unsafe broad activation
- missing eval examples

Adopt: line-level diagnostics and skillset coherence checks.

Reject: public registry dependence for core Jarvis behavior.

## Priority Backlog

1. Add `/cli-advances` as a console surface that explains this backlog and current Jarvis status. Status: done.
2. Add `.agents/skills` export preview with no writes by default. Status: done through `jarvis --skills-export-preview`.
3. Add `/sessions`, `/resume`, and `/fork` for terminal continuity.
4. Add `/mission-control` with daemon, tasks, approvals, dirty git state, context budget, and packaged-app identity.
5. Add `--skill-audit` for local skill diagnostics. Status: done through `jarvis --skill-audit`.
6. Add a terminal transcript export command for debugging and sharing Jarvis failures.

## Sources

- GitHub Changelog: https://github.blog/changelog/2026-04-16-manage-agent-skills-with-github-cli/
- GitHub CLI manual: https://cli.github.com/manual/gh_skill
- GitHub CLI `gh skill install`: https://cli.github.com/manual/gh_skill_install
- OpenCode CLI docs: https://open-code.ai/en/docs/cli
- Claude Code architecture analysis: https://arxiv.org/abs/2604.14228
- Skilldex paper: https://arxiv.org/abs/2604.16911
