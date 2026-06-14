# ADE / orchestrate.py → Native Claude Code 2.1 Migration Map

## 1. Feature Mapping Table

| Custom feature | Native equivalent | Coverage | Recommended action |
|---|---|---|---|
| `orchestrate.py dispatch <lane> --prompt` | `claude --worktree <name>` (optionally `--tmux`) | Full | adopt-native |
| `orchestrate.py status` | `claude agents` (Agent View — groups by needs-input / working / completed) | Full | adopt-native |
| `orchestrate.py harvest <lane> [--yes]` | `git merge` after agent completes; `--yes` replaced by AUTO MODE approval flow | Full | adopt-native |
| `orchestrate.py abort <lane>` | Close the agent session / `TaskStop` from Agent View | Full | adopt-native |
| `ade_cmd.py start <lane> --prompt` | `claude --worktree <name> -p "<prompt>"` | Full | adopt-native |
| `ade_cmd.py list` | `claude agents` root-dir view | Full | adopt-native |
| `ade_cmd.py watch` | `claude agents` live Agent View (auto-refreshes) | Full | adopt-native |
| `ade_cmd.py sync` | `git merge` / worktree cleanup after session ends | Full | adopt-native |
| `ade_cmd.py stop` | Session close from Agent View or `TaskStop` | Full | adopt-native |
| `ade_cmd.py approvals` | AUTO MODE (shift+tab) — classifier-gated, replaces manual approval gate | Full | adopt-native |
| `ADE_AUTO_APPROVE_PLAN` env flag | AUTO MODE — plan gate retired on models 4.6+; no plan step needed | Full | adopt-native |
| `ADE_CLAUDE_SKIP_PERMISSIONS` env flag | AUTO MODE — safer classifier-gated equivalent; --dangerously-skip-permissions still available but AUTO MODE preferred | Full (safer) | adopt-native |
| `ADE_TEST_CMD` env flag | `/goal` directive — express completion condition; auto mode runs narrowest verify available | Partial | hybrid (pass test cmd via /goal wording) |
| `ADE_SKIP_VERIFY` env flag | Omit `/goal` test spec; agent stops after implement phase | Full | adopt-native |
| Per-lane git worktree isolation | `isolation: worktree` subagent config; `claude --worktree <name>` | Full | adopt-native |
| Per-lane tmux session | `--tmux` flag or Agent View manages sessions natively | Full | adopt-native |
| Plan→Execute→Verify→Retry (3 max) loop | AUTO MODE + `/go` (test end-to-end + /simplify + PR); retry built into agent loop | Full | adopt-native |
| Main branch protection (touch only at harvest) | Worktree model: main never touched until explicit merge; same guarantee natively | Full | adopt-native |
| Fan-out across many lanes | DYNAMIC WORKFLOWS: "use a workflow" trigger → orchestrator + implementer + verifiers + fixer; hundreds of parallel agents | Full | adopt-native |
| Recurring orchestration runs | `/loop` (local, up to 3 days) or `/schedule` (cloud, laptop-closed) | Full | adopt-native |
| Mobile / remote dispatch | Claude mobile app Code tab; `claude remote-control`; `--teleport`; iMessage plugin | Full | adopt-native |
| Compounding quality / institutional memory | Write every mistake/pattern to CLAUDE.md; turn repeated tasks into skills | Full | adopt-native |
| Frontend verify loop | `/go` + Chrome extension (visual end-to-end verification) | Full | adopt-native |

---

## 2. Keep Custom — Genuinely Jarvis-Specific, No Native Equivalent

These two surfaces serve the **Jarvis product**, not the dev workflow. Native Claude Code primitives replace ADE's dev-orchestration layer but have no opinion on these:

### 2a. Local Ollama Roster Agent Dispatch (`task_runtime/agent_dispatch`)

Jarvis dispatches work to a fleet of **local Ollama-hosted specialist agents** at runtime — not to Claude Code subagents. This is a product feature: the user's voice command or chat message is routed to whichever local model is best suited (code, search, summarize, etc.).

- No native Claude Code primitive covers local-LLM fan-out at the product/UX level.
- Keep `task_runtime/agent_dispatch` and the Ollama roster configuration.
- The only integration point: Claude Code agents (dev workflow) may call into this dispatch system as a tool, but they do not replace it.

### 2b. Jarvis Agent-OS Dashboard

The real-time dashboard showing active Jarvis agents, their statuses, memory consumption, and task queues is a **user-facing product surface** inside the macOS desktop app (`ui.py`, `AGENT_BOARD.md`).

- `claude agents` is a developer tool for inspecting Claude Code sessions. It is not a user-facing product UI.
- Keep the Agent-OS dashboard code. It surfaces Jarvis's own runtime state to the user, not Claude Code session state.

---

## 3. Phase-Out Plan (5 Steps)

**Step 1 — Turn on AUTO MODE**
Switch all `ADE_AUTO_APPROVE_PLAN` and `ADE_CLAUDE_SKIP_PERMISSIONS` usages to AUTO MODE (shift+tab in Claude Code). Retire the permission-bypass env flags. This is the single highest-leverage safety improvement.

**Step 2 — Adopt `claude --worktree` + Agent View**
Replace `ade_cmd.py start/list/watch/stop` with native `claude --worktree <name>` invocations and monitor via `claude agents`. Delete `ade_cmd.py` once all active lanes are migrated.

**Step 3 — Replace `orchestrate.py dispatch`**
Rewrite any script that calls `orchestrate.py dispatch` to invoke `claude --worktree <name> -p "<prompt>"` directly. Map `--tests` to `/goal` wording. Map `--skip-verify` to omitting the goal. Retire `orchestrate.py`.

**Step 4 — Adopt Dynamic Workflows for fan-out**
Any multi-lane fan-out that currently needs N parallel `dispatch` calls should be expressed as a single "use a workflow" prompt to an orchestrator agent. Let Claude Code spawn implementers, verifiers, and fixers natively. Remove the manual lane-multiplication logic.

**Step 5 — Retire ADE tmux/loop infrastructure, keep only Jarvis-product code**
Delete `ade_cmd.py`, `orchestrate.py`, and any ADE env-flag scaffolding. Replace recurring runs with `/schedule` or `/loop`. What remains in the repo should be only: Jarvis runtime (`task_runtime/agent_dispatch`, the Agent-OS dashboard, `ui.py`, `voice.py`, etc.) — the product, not the dev-workflow tooling.
