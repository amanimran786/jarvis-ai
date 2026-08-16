# Codex-Controlled Engineering Orchestration

Codex is the sole Jarvis engineering control plane. It chooses the roadmap
item, defines the contract, approves any POC, assigns a worker, reviews the
result, and decides when work is complete. Claude and local models are bounded
workers or proposal generators. They do not select, assign, approve, or
complete work.

`harness.agent_coordinator` is the only supported writer for coordination
transitions in `WORK_QUEUE.json`. The shared checkout permits one active
engineering lease. True parallel implementation requires isolated worktrees
and a Codex-owned merge arbiter.

## Lifecycle

```text
local suggestion -> proposed
                       |
                       v
Codex selection -> assigned -> in_progress -> awaiting_codex_review -> done
                       |             |                  |
                       |             |                  +-> needs_review
                       |             +-> unverified / blocked
                       +-> awaiting_approval (independent human safety gate)
```

Every coordination-v2 assignment binds the worker, orchestration stage, clean
base commit, task-spec digest, and safety-contract digest. An implementation
that depends on a POC also binds the accepted POC digest. Mutation after
assignment invalidates the assignment.

## Codex Controller Commands

Assign exactly one selected task:

```bash
./venv/bin/python -m harness.agent_coordinator assign \
  --task-id TASK_ID \
  --worker claude \
  --stage implementation \
  --rationale "Why this is the next roadmap item" \
  --json
```

Use `--stage poc` for a bounded proof of concept. For an implementation whose
contract has `constraints.poc_required: true`, pass the accepted POC with
`--parent-task-id POC_TASK_ID`. POC acceptance is a Codex engineering decision;
it is not a substitute for the human side-effect approval gate.

Review a verified worker submission:

```bash
./venv/bin/python -m harness.agent_coordinator review \
  --task-id TASK_ID \
  --decision accept \
  --summary "Why the evidence and implementation satisfy the contract" \
  --json
```

Use `--decision reject` when the candidate needs more work. Only an accepted
Codex review can move any worker result to `done`.

## Worker Commands

A worker claims only its exact Codex assignment:

```bash
./venv/bin/python -m harness.agent_coordinator claim \
  --agent claude \
  --lease-seconds 3600 \
  --json
```

The claim fails closed when the assignment worker, base commit, task digest,
contract digest, POC digest, safety approval, or lease state no longer matches.
Workers cannot inspect the queue and choose a different item.

Renew a long-running lease:

```bash
./venv/bin/python -m harness.agent_coordinator heartbeat \
  --agent claude \
  --task-id TASK_ID \
  --lease-id LEASE_ID \
  --lease-seconds 3600 \
  --json
```

Submit clean, committed work:

```bash
./venv/bin/python -m harness.agent_coordinator finish \
  --agent claude \
  --task-id TASK_ID \
  --lease-id LEASE_ID \
  --summary "What changed and which tests passed" \
  --json
```

`finish` runs deterministic verification. Every successful worker submission,
including a Codex implementation session, moves to `awaiting_codex_review`, not
`done`. The separate Codex review transition is the only completion path.

Release unfinished work without selecting a replacement:

```bash
./venv/bin/python -m harness.agent_coordinator release \
  --agent claude \
  --task-id TASK_ID \
  --lease-id LEASE_ID \
  --reason "Concrete blocking condition" \
  --json
```

Record rate-limit cooldown:

```bash
./venv/bin/python -m harness.agent_coordinator cooldown \
  --agent claude \
  --seconds 3600 \
  --reason "Claude session limit" \
  --json
```

Cooldown releases the assignment back to Codex. Another worker cannot take it
over without a fresh Codex assignment.

## Local Model Policy

- Local LLM output may propose a POC, task, patch, or follow-up.
- Codex must convert an accepted proposal into a typed, digest-bound assignment.
- Local models may generate code inside an already assigned Claude or Codex
  work session, but their output receives the same review and test gates.
- Paid cloud fallback is disabled unless Aman explicitly authorizes it.
- Unsupported legacy provider-named task IDs remain only as audit history.

## Hard Invariants

- Codex is the only roadmap selector and assignment authority.
- A worker submission never implies acceptance or completion.
- Local-model suggestions never become executable queue entries automatically.
- Human side-effect approval and Codex POC approval are separate digest-bound
  decisions.
- Active rows without a valid lease are quarantined instead of consuming
  capacity forever.
- One locked writer owns every queue transition; direct JSON edits are
  unsupported.
- Completion records preserve `executed_by` separately from `completed_by`.

## Trust Boundary

The current CLI enforces workflow roles and auditability, not hostile-process
isolation. `--agent codex` is self-asserted by any process with shell and file
access. Production isolation requires a local broker that holds controller
credentials, gives workers capability-scoped assignment tokens, and prevents
workers from writing authoritative queue files directly. Until that broker is
built, scheduled Claude workers must use the deployed assignment-only skill and
must not receive controller instructions or credentials.

Inspect coordination state with:

```bash
./venv/bin/python -m harness.agent_coordinator status --json
```

Runtime state lives at:

```text
~/Library/Application Support/Jarvis/orchestrator/agent_coordination.json
```

The canonical Claude worker contract is
`scripts/jarvis-autonomous-orchestrator.SKILL.md`. Its deployed copy is
`~/Claude/Scheduled/jarvis-autonomous-orchestrator/SKILL.md`.
