# Claude/Codex Queue Coordination

Jarvis uses one atomic lease protocol for Claude and Codex. The protocol is
implemented by `harness.agent_coordinator` and operates on `WORK_QUEUE.json`
under the existing cross-process queue lock.

## Why this exists

The Cowork scheduled runtime does not expose `start_code_task`. Repeated
scheduled runs therefore logged activity but dispatched no work. The scheduled
Claude session now executes one leased task itself instead of trying to spawn a
missing subtask.

The shared repository checkout permits one active engineering lease. Running
two coding agents in the same directory would allow overlapping edits and Git
index races. Parallel execution must use isolated worktrees and a merge arbiter;
that is intentionally outside this first failover implementation.

## Agent lifecycle

Claim the highest-priority eligible task:

```bash
./venv/bin/python -m harness.agent_coordinator claim \
  --agent codex \
  --takeover-cooling \
  --lease-seconds 3600 \
  --json
```

Use `--agent claude` from Cowork. A claim succeeds only when:

- the shared checkout is clean;
- no other active lease exists;
- the task has a valid, matching typed contract;
- any required approval is bound to the exact contract and task digests;
- the task is unassigned, assigned to the caller, or assigned to an agent that
  is explicitly cooling down.

The lease stores both digests. Completion combines queue scope with contract
output paths and combines queue verification commands with the contract entry
point, so sparse legacy queue rows cannot weaken the verifier.

Renew a lease before a long test or commit:

```bash
./venv/bin/python -m harness.agent_coordinator heartbeat \
  --agent codex \
  --task-id TASK_ID \
  --lease-id LEASE_ID \
  --lease-seconds 3600 \
  --json
```

After committing and returning the checkout to a clean state, submit completion:

```bash
./venv/bin/python -m harness.agent_coordinator finish \
  --agent codex \
  --task-id TASK_ID \
  --lease-id LEASE_ID \
  --summary "What changed and which tests passed" \
  --json
```

`finish` collects deterministic evidence through `completion_verifier` and only
marks the queue row done when scope and verification checks pass. Agents do not
mark their own tasks done directly.

Release unfinished work:

```bash
./venv/bin/python -m harness.agent_coordinator release \
  --agent codex \
  --task-id TASK_ID \
  --lease-id LEASE_ID \
  --reason "Blocked by missing dependency" \
  --json
```

## Cooldown handoff

When an agent receives a rate-limit, credit, or session-limit error, record it:

```bash
./venv/bin/python -m harness.agent_coordinator cooldown \
  --agent claude \
  --seconds 3600 \
  --reason "Claude credit or session limit" \
  --json
```

This releases the agent's active lease and makes an assigned task eligible for
the other agent when it claims with `--takeover-cooling`. If an agent disappears
without recording cooldown, lease expiry requeues the task and starts a
conservative 20-minute cooldown automatically.

Clear a recovered agent early:

```bash
./venv/bin/python -m harness.agent_coordinator clear-cooldown \
  --agent claude \
  --json
```

Inspect current state:

```bash
./venv/bin/python -m harness.agent_coordinator status --json
```

Coordination state lives at:

```text
~/Library/Application Support/Jarvis/orchestrator/agent_coordination.json
```

The canonical Cowork task prompt is
`scripts/jarvis-autonomous-orchestrator.SKILL.md`. Its deployed copy is
`~/Claude/Scheduled/jarvis-autonomous-orchestrator/SKILL.md`.
