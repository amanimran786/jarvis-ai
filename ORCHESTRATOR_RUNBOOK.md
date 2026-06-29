# Orchestrator Runbook

This Claude session is the **conductor** over parallel autonomous agents. Each
agent is an ADE lane: an isolated git worktree + tmux session running a real
`claude` agent on a Plan → Execute → Verify → Retry loop. The conductor plans
lanes, dispatches them, monitors, and harvests — `main` is only touched at harvest.

The loop is the product. The coding agent is a replaceable reasoning component
inside it. Prompts are rendered from durable loop state; they are not the place
where retry policy, completion criteria, safety boundaries, or workflow state live.

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

## Codex architecture addendum: design loops, not prompts

The current `--prompt` interface is useful as a compatibility layer, but it is
too weak to be the orchestration contract. A long prompt can describe a workflow;
it cannot reliably enforce one. The conductor should compile each request into a
typed task contract, then let the loop generate the smallest phase-specific prompt
for whichever agent is active.

```text
TaskSpec
  goal
  scope: allowed_files + forbidden_files
  acceptance: observable outcomes
  verification: commands + required evidence
  constraints: local_first + security + packaging
  budget: attempts + wall_time + tool_calls
        │
        ▼
Context builder → Planner → Plan gate → Executor → Evidence collector → Verifier
                       ▲                                      │             │
                       └──────── Repair policy ◀ Failure classifier ◀───────┘
                                                              │
                                                   done / retry / escalate
```

### Ownership boundary

The **loop owns**:

- context assembly and freshness
- file/tool permissions and worktree isolation
- phase transitions, checkpoints, budgets, and timeouts
- deterministic verification commands and evidence capture
- failure classification and retry strategy
- completion, escalation, and harvest eligibility

The **agent owns**:

- proposing a plan within the supplied contract
- choosing the next implementation action from allowed tools
- interpreting evidence and proposing a repair
- explaining uncertainty when the contract cannot be satisfied

An agent response is a proposal, never proof of completion. Only loop-observed
artifacts — diff, command exit codes, test output, package smoke results, and policy
checks — may advance the lane to `DONE`.

### Gaps in the current ADE loop

| Current behavior | Loop-owned replacement |
|---|---|
| `phase_plan()` asks for free-form `PLAN.md`, then unattended mode auto-approves it | Parse a structured plan and reject files, tools, or steps outside `TaskSpec` |
| `phase_execute()` does not use the agent process return code to control state | Record exit status and classify agent crash/timeout separately from test failure |
| Retries receive the last 3,000 characters of raw test output | Persist full evidence, classify the failure, and render a targeted repair packet |
| Missing or skipped tests return success | Require explicit evidence appropriate to the task type; `skip_verify` means `UNVERIFIED`, not `DONE` |
| Test success alone marks the lane done | Require expected diff, scope compliance, verification evidence, and no unresolved policy findings |
| Retry always re-invokes the same coding agent | Route by failure class: implementation agent, test analyst, security reviewer, or human escalation |
| Prompt text carries task scope | Enforce allowed paths and commands outside the model, then report violations as evidence |

### Minimal implementation sequence

1. Add a serializable `TaskSpec` and `AttemptRecord`; keep `--prompt` as an adapter
   that populates `goal` only for backward compatibility.
2. Persist one checkpoint after every phase with input hashes, agent/model identity,
   changed files, command results, and remaining budget.
3. Add a deterministic plan gate and completion gate before execution and `DONE`.
4. Add failure classes (`agent_error`, `scope_violation`, `test_failure`,
   `verification_missing`, `policy_failure`, `infrastructure_failure`) with distinct
   retry or escalation rules.
5. Split executor and verifier roles. The verifier reads the diff and evidence but
   does not inherit the executor's claims.
6. Make harvest consume the checkpoint/evidence record, not a prose completion
   message or an empty `git diff HEAD` view.

This is the architectural shift: stop improving the mega-prompt and improve the
state machine that decides what the next agent sees, what it may do, and what
evidence is required before another transition.

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

1. **Compile lane contracts.** Decompose the goal into *disjoint, file-scoped*
   `TaskSpec` records. Two lanes must not edit the same file (see AGENT_BOARD.md
   lane convention). Include observable acceptance criteria, not instructions to
   claim completion.
2. **Scope verification.** Every code lane gets deterministic commands and
   required evidence. A no-code lane may use `--skip-verify`, but remains
   explicitly unverified until its artifact is inspected.
3. **Dispatch** contracts. The compatibility CLI renders `--prompt`; the target
   loop renders phase-specific prompts from the persisted contract and checkpoint.
4. **Monitor** checkpoints with `orchestrate status`. On failure, inspect the
   recorded failure class and evidence before choosing retry, specialist reroute,
   or escalation. Do not merely re-dispatch with a larger prompt.
5. **Harvest** each eligible lane: `harvest <lane>` (review evidence and diff),
   then `--yes` (merge). Harvest one lane at a time so conflicts are attributable.
6. **Record** on AGENT_BOARD.md: claim/close the lane with a timestamped line.

## Safety model

- **Isolation:** a lane edits only its own worktree branch. A running agent can
  never write to `main` — only `harvest --yes` merges, and only after you review.
- **Permissions:** unattended lanes run `claude --dangerously-skip-permissions`
  (set by default; `--supervised` opts out). Inside its worktree the agent runs
  tools/shell with no per-action gate. Blast radius = that branch. Do not dispatch
  a contract whose allowed actions you would not run unsupervised.
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
