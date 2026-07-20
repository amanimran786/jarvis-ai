# Orchestrator Runbook

> **Two unrelated systems share the word "orchestrator" in this repo.** This
> file documents both — read the section that matches what you're touching:
> - **Session Dashboard & Autonomous Loop** (`session_orchestrator.py`,
>   `orchestrator.py`, `orchestrator_loop.py`) — file-based dev-session
>   coordination + a scheduled loop that harvests/dispatches `WORK_QUEUE.json`
>   tasks automatically. Covered immediately below.
> - **ADE Lanes** (`orchestrate.py`) — manual tmux + git-worktree lane
>   dispatch, described in "Codex architecture addendum" onward further down
>   this file.
>
> They do not call into each other. `orchestrate.py` never touches
> `session_orchestrator.py`, and the autonomous loop never spawns a tmux lane.

## Session Dashboard & Autonomous Loop

### What each file does

- **`session_orchestrator.py`** — a read-only terminal dashboard (rich UI,
  falls back to plain ANSI if `rich` isn't installed) for watching dev
  sessions and the work queue. Reads `ORCHESTRATOR_STATUS.json` (session
  health/`last_active`), `WORK_QUEUE.json` (task queue), and `SESSIONS.json`
  (registry of named lanes + their Claude session IDs). Writes activity lines
  to `MASTER_LOG.md`. Its `watch` command is a separate hourly rate-limit
  watchdog: on each UTC top-of-hour it checks whether any of the four named
  dev lanes (`jarvis-board`, `jarvis-self-eval`, `jarvis-local-llm`,
  `jarvis-audit`) is stalled and, if so, writes `RESUME_SIGNAL.json` so those
  sessions can resume.

- **`orchestrator.py`** — primarily the LLM intent classifier (`classify()`)
  that `router.py` uses to route chat input to tools; unrelated to session
  coordination. Its `if __name__ == "__main__":` block is a thin alias:
  running `python orchestrator.py <cmd>` imports `session_orchestrator.main`
  and calls it — so `python orchestrator.py status` is identical to
  `python session_orchestrator.py status`. Nothing else in `orchestrator.py`
  touches the dashboard or the loop.

- **`orchestrator_loop.py`** — the autonomous single-iteration loop. It is
  invoked every 5 minutes by the `com.jarvis.loop` launchd job
  (`scripts/com.jarvis.loop.plist` → `harness/cowork_launcher.py` →
  `orchestrator_loop.run_loop()`). Each call: expires sessions stalled >90 min
  (`ACTIVE_SESSIONS.json` via `harness/session_tracker.py`), harvests
  completed sessions (verifying evidence with
  `harness/completion_verifier.py` + `harness/task_contract.py`, applying
  `harness/retry_policy.py` on failure), asks a local LLM for follow-up
  tasks, picks the next queued task up to `--max-concurrent`, validates its
  typed `TaskContract` (approval via `harness/approval_workflow.py`,
  capabilities via `harness/capability_checker.py`), renders a prompt
  (`harness/prompt_generator.py`), and writes a launch record to
  `LAUNCH_QUEUE.json` for the Cowork companion to actually start the session.

**Important:** `orchestrator_loop.py` tracks sessions for harvest/retry via
`ACTIVE_SESSIONS.json` (`harness/session_tracker.py`) — a completely
different registry from the `ORCHESTRATOR_STATUS.json` / `SESSIONS.json` pair
that `session_orchestrator.py`'s dashboard reads. A lane can be "active" in
one and stale or absent in the other. The one file both sides actually share
is `WORK_QUEUE.json` — that's the real integration point, documented in
`WORK_QUEUE_SCHEMA.md`.

### Invocation

```bash
# Dashboard (read-only, human-facing)
python session_orchestrator.py              # live dashboard, refresh 30s
python session_orchestrator.py status        # one-shot status, then exit
python session_orchestrator.py register <session-name> [next-task]
python session_orchestrator.py add-task <session> <task> <priority>
python session_orchestrator.py history       # tail MASTER_LOG.md
python session_orchestrator.py watch         # hourly rate-limit watchdog, 60s poll

# Equivalent — delegates straight to session_orchestrator.main()
python orchestrator.py status

# One iteration of the autonomous loop (normally launchd's job — see below)
python orchestrator_loop.py --max-concurrent 3 [--dry-run] [--verbose]
```

Environment variables:
- `JARVIS_ORCHESTRATOR_ATTEMPT_LOG` — override path for `attempts.jsonl`
  (default `~/Library/Application Support/Jarvis/orchestrator/attempts.jsonl`).

### How it integrates with WORK_QUEUE.json

- The `com.jarvis.loop` launchd job runs every 300s and calls
  `orchestrator_loop.run_loop()` — this is the production cadence; once the
  job is loaded, nothing needs to be invoked manually for tasks to be
  harvested and dispatched.
- `run_loop()` also starts `jarvis_dashboard` (uvicorn, port 7842) in a daemon
  thread the first time it runs in a process — idempotent across iterations,
  logged and swallowed if the port is already taken.
- `session_orchestrator.py`'s dashboard is read-only with respect to the
  loop: `add-task` and `register` write `WORK_QUEUE.json` /
  `ORCHESTRATOR_STATUS.json` directly and are picked up by the loop on its
  next poll — there is no direct call from one script into the other.

### Common dev workflows

**Watch dev lanes and the queue:**
```bash
python session_orchestrator.py
```

**Register a manual dev lane so it shows on the dashboard** (e.g. a Cowork
session that isn't running `main.py` and wouldn't otherwise appear):
```bash
python orchestrator.py register jarvis-board "Wire context window budget"
```

**Queue a task for a named lane:**
```bash
python orchestrator.py add-task jarvis-board "Fix flaky STT test" 1
```

**Dry-run the autonomous loop** (no state mutation, useful for debugging
prompt generation or contract resolution):
```bash
python orchestrator_loop.py --dry-run --verbose
```

**Run the loop for real, manually** (normally launchd's job — only do this to
force an iteration between the 5-minute launchd cadence):
```bash
python orchestrator_loop.py --max-concurrent 3
```

**Check history:**
```bash
python session_orchestrator.py history     # tails MASTER_LOG.md
tail -f logs/orchestrator.log              # session_orchestrator's own log
```

**Bootstrap orchestration from scratch (fresh checkout):**
1. `WORK_QUEUE.json` starts as `[]` if missing — seed it with
   `python orchestrator.py add-task <lane> "<first task>" 1`, or hand-edit it
   per the schema in `WORK_QUEUE_SCHEMA.md` (a code task needs
   `allowed_files`, `acceptance_criteria`, and `verification_commands`; a
   no-code task can skip verification per that schema's status lifecycle).
2. Load the scheduled loop: `launchctl load scripts/com.jarvis.loop.plist`
   (runs every 5 min; logs to `logs/launchd.log` / `logs/launchd_error.log`).
3. Watch it work: `python session_orchestrator.py` (dashboard), or
   `tail -f MASTER_LOG.md` / `tail -f logs/orchestrator.log`.
4. For the four named dev lanes, also run `python session_orchestrator.py
   watch` (or load it as its own launchd job) so `RESUME_SIGNAL.json` gets
   written on hourly rate-limit resets.

### Troubleshooting

- **Dashboard says "No sessions in ORCHESTRATOR_STATUS.json yet"** — no
  session has called `register` (or `harness/audit.py`'s `start_session`)
  yet. This file is disk-only and gitignored, so a fresh checkout always
  starts empty; it is not an error.
- **Task stuck in `blocked` / `awaiting_approval`** — `orchestrator_loop.py`
  requires a typed `TaskContract` (`harness/task_contract.py`,
  `contract_for_task`) resolvable via `contract_id`, the derived `TaskSpec`
  id, or `session_name`. A queue row with no matching contract is blocked
  with "autonomous execution requires an explicit typed task contract" — add
  or fix the contract, don't just retry the task.
- **Task shows `unverified` after a session claims completion** —
  `harness/completion_verifier.py` couldn't collect evidence (missing
  `repo_path`/`base_ref` on the session record, or the verification commands
  didn't produce the expected evidence). Check `verification_reasons` on the
  task entry in `WORK_QUEUE.json`.
- **A lane looks STALLED on the dashboard but seems fine otherwise** —
  `_session_health()` in `session_orchestrator.py` only applies the 5-minute
  stall threshold to sessions reporting `status: active`; check what's
  writing `last_active` into `ORCHESTRATOR_STATUS.json` for that lane. Also
  remember this is a different liveness signal than
  `orchestrator_loop.py`'s own `ACTIVE_SESSIONS.json` — a lane can be fine in
  one and stale in the other.
- **`python orchestrator.py <cmd>` behaves unexpectedly** — it's an alias
  straight into `session_orchestrator.main()`; it has nothing to do with the
  `classify()` intent-routing logic that makes up the rest of
  `orchestrator.py`.
- **Follow-up task suggestions never appear** — `_suggest_follow_ups()` calls
  `brains.brain_ollama.ask_local`; if Ollama isn't running or the configured
  model isn't pulled, it logs a warning and returns `[]` — the loop keeps
  going, it just won't auto-enqueue follow-ups for that completion.
- **`MASTER_LOG.md` isn't growing even though the loop is clearly running** —
  by design: `_run_loop` buffers each iteration's log lines and drops them if
  the iteration did nothing (harvested/follow_ups/launched/blocked/
  unverified/rejected/retried all zero), so idle polling every 5 minutes
  doesn't bloat the file.
- **Dashboard renders plain text instead of tables/colors** — `rich` isn't
  importable in the active interpreter; this is an automatic fallback, not a
  bug. `pip install rich` in the same venv if you want the rich UI.

---

## ADE Lanes (`orchestrate.py`)

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
