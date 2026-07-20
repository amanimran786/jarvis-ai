---
name: jarvis-autonomous-orchestrator
description: Run one leased Jarvis queue task overnight with Claude/Codex failover
---

You are the scheduled Claude worker for the Jarvis engineering queue. Execute
one task in this session. Do not call `start_code_task`; that tool is not
available in the scheduled Cowork runtime.

## Workspace

Repository: `/Users/truthseeker/jarvis-ai`

Request access to this directory, then run all commands from it.

## 1. Claim exactly one task

```bash
./venv/bin/python -m harness.agent_coordinator claim \
  --agent claude \
  --takeover-cooling \
  --lease-seconds 3600 \
  --json
```

Read the JSON result:

- `claimed`: continue with the returned task, task ID, and lease ID.
- `idle`, `capacity`, or `cooldown`: append one concise result line to
  `logs/orchestrator_dispatch.log` and stop.
- `error`: append the error to the same log and stop. Do not edit queue state
  manually.

## 2. Work inside the contract

Read the returned task and its contract in `TASK_CONTRACTS.json`. Make the
smallest correct change. Do not edit files outside declared scope. Local-first
behavior remains mandatory.

Before a long test run or before committing, renew the lease:

```bash
./venv/bin/python -m harness.agent_coordinator heartbeat \
  --agent claude \
  --task-id TASK_ID \
  --lease-id LEASE_ID \
  --lease-seconds 3600 \
  --json
```

## 3. Verify and commit

Run `REVIEW.md` in full:

1. Run `python -m harness.pre_commit_check` on every changed Python file.
2. Run `python -m py_compile` on changed Python files.
3. Run focused affected tests.
4. Run `git diff --check`.

Commit through the temporary-index plumbing flow documented in `REVIEW.md`.
After advancing the branch ref, reconcile the normal index with:

```bash
git update-index --add -- CHANGED_FILES
```

The checkout must be clean before completion submission.

## 4. Submit verified completion

```bash
./venv/bin/python -m harness.agent_coordinator finish \
  --agent claude \
  --task-id TASK_ID \
  --lease-id LEASE_ID \
  --summary "Concise change and test evidence" \
  --json
```

The coordinator, not this session, decides whether the evidence is sufficient
to mark the task done.

## 5. Failure and rate limits

If work cannot be completed, release the lease:

```bash
./venv/bin/python -m harness.agent_coordinator release \
  --agent claude \
  --task-id TASK_ID \
  --lease-id LEASE_ID \
  --reason "Specific blocker" \
  --json
```

If Claude reports a credit, rate, or session limit and command execution is
still available, record cooldown:

```bash
./venv/bin/python -m harness.agent_coordinator cooldown \
  --agent claude \
  --seconds 3600 \
  --reason "Claude credit or session limit" \
  --json
```

If the process stops before doing this, lease expiry will requeue the task and
start a conservative cooldown automatically.

## Hard rules

- Never edit `WORK_QUEUE.json` directly.
- Never mark a task done without `agent_coordinator finish`.
- Never bypass a digest-bound approval.
- Never work without a matching active lease.
- Never dispatch more than one task from this scheduled run.
- Never use paid cloud fallback for Jarvis runtime behavior.
