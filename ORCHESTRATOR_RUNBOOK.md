# Orchestrator Runbook

This Claude session is the **conductor** over parallel autonomous agents. Each
agent is an ADE lane: an isolated git worktree + tmux session running a real
`claude` agent on a Plan → Execute → Verify → Retry loop. The conductor plans
lanes, dispatches them, monitors, and harvests — `main` is only touched at harvest.

```
CONDUCTOR (this chat)
  orchestrate dispatch lane-1 --prompt "…" --tests "…"
  orchestrate dispatch lane-2 --prompt "…" --tests "…"
        │
        ├─ each lane → .worktrees/<lane>  (branch ade/<lane>)  + tmux ade-<lane>
        │     claude -p  →  Plan(auto) → Execute → Verify(scoped) → Retry×3
        │
  orchestrate status            # liveness, diff size, ready-to-sync
  orchestrate harvest <lane>    # review diff; --yes merges to main
```

## Command set

```bash
# Dispatch an autonomous lane (scoped tests — the safe default)
python3 orchestrate.py dispatch <lane> \
  --prompt "<self-contained task; name exact files; 'touch no other file'>" \
  --tests  "python3 -m pytest tests/test_<x>.py -q"

# No-code lane (docs, config): skip the test phase
python3 orchestrate.py dispatch <lane> --prompt "…" --skip-verify

# Supervised lane (agent must ask permission per tool — NOT unattended)
python3 orchestrate.py dispatch <lane> --prompt "…" --tests "…" --supervised

# Watch everything
python3 orchestrate.py status

# Inspect one lane's diff, then merge it
python3 orchestrate.py harvest <lane>          # shows diff --stat, does NOT merge
python3 orchestrate.py harvest <lane> --yes    # runs `ade sync` → merge to main

# Kill a lane + remove its worktree (discards uncommitted work)
python3 orchestrate.py abort <lane>

# Drop into a live agent to watch it think
ade watch <lane>     # (tmux attach; Ctrl-b d to detach)
```

## The operating loop (what the conductor does each cycle)

1. **Plan lanes.** Decompose the goal into *disjoint, file-scoped* lanes. Two
   lanes must not edit the same file (see AGENT_BOARD.md lane convention). Write
   each lane a self-contained prompt: it has no memory of this chat.
2. **Scope the tests.** Every lane gets `--tests "<narrow pytest>"` or
   `--skip-verify`. Never leave it unscoped — that runs the full repo suite
   (~15 min) and trips known order-dependent flakes into retry storms.
3. **Dispatch** all lanes.
4. **Monitor** with `orchestrate status`. A lane is `▲ ready_to_sync` when it is
   `DONE` with a non-empty diff. `FAILED` = 3 retries exhausted; read the pane
   (`ade watch`) or re-dispatch with a sharper prompt.
5. **Harvest** each ready lane: `harvest <lane>` (review), then `--yes` (merge).
   Harvest one lane at a time so conflicts are attributable.
6. **Record** on AGENT_BOARD.md: claim/close the lane with a timestamped line.

## Safety model

- **Isolation:** a lane edits only its own worktree branch. A running agent can
  never write to `main` — only `harvest --yes` merges, and only after you review.
- **Permissions:** unattended lanes run `claude --dangerously-skip-permissions`
  (set by default; `--supervised` opts out). Inside its worktree the agent runs
  tools/shell with no per-action gate. Blast radius = that branch. Don't dispatch
  prompts you wouldn't run unsupervised.
- **Harvest is the gate.** Review every diff before `--yes`. The post-tool hook
  is scoped to the agent's changed files, so the diff is the real change.

## Env flags (set automatically by orchestrate.py — reference only)

| Flag | Effect |
|------|--------|
| `ADE_AUTO_APPROVE_PLAN=1` | Skip the human plan-approval `input()` gate (always set by dispatch) |
| `ADE_CLAUDE_SKIP_PERMISSIONS=1` | Inject `--dangerously-skip-permissions` (default on; off with `--supervised`) |
| `ADE_TEST_CMD="…"` | Scope the verify phase to this command (`--tests`) |
| `ADE_SKIP_VERIFY=1` | Skip the verify phase entirely (`--skip-verify`) |

## Gotchas

- **tmux required** (`brew install tmux`, installed 2026-06-13). No tmux → lanes
  can't spawn.
- **`claude` binary** must be on PATH (`/Users/truthseeker/.local/bin/claude`).
- **Worktree HEAD is committed state**, not your dirty working tree. A lane
  branches from the last commit — uncommitted edits in main are NOT visible to it.
- **A FAILED lane keeps its worktree** for inspection. `abort` to clean up.
- **Don't commit this file or AGENT_BOARD.md** without Aman's OK (coordination
  docs policy, AGENT_BOARD item 8).
```
