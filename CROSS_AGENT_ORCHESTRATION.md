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

---

## Parallel Claude + Codex — Branch Strategy

CI is now green (Item 1 complete). Both agents can work simultaneously by using
**isolated branches** and merging via pull-request after CI passes.

### Branch naming

```
claude/roadmap-N-short-name    # Claude sessions
codex/roadmap-N-short-name     # Codex sessions
```

Examples:
```
claude/roadmap-15-selflearn-fix
codex/roadmap-3-orchestrator-watchdog
```

### Merge protocol

1. Create branch off `main`
2. Make changes, commit (following REVIEW.md gate)
3. Push branch — GitHub Actions runs CI
4. **CI must be green before merge** — never merge a red branch
5. Merge via `git merge --no-ff` (preserves branch history)
6. Delete merged branch

### Lane assignments (current)

Claude owns:
- Item 1.5 — self-learning pipeline fix (fusion bug, promotion, telemetry)
- Item 2 — Dashboard launchd fix
- Item 4 — Wire `run_checks()` into orchestrator loop
- Item 6 — Security review
- Item 9 — Full 24/7 autonomous operation

Codex owns:
- Item 3 — Orchestrator self-healing via launchd KeepAlive
- Item 5 — Specialist model routing (devstral, qwen3:30b-a3b)
- Item 7 — Test coverage hardening
- Item 8 — Voice pipeline production ready

### Conflict avoidance rules

- Items in the same lane do not overlap — each agent works one item at a time
- Shared files (`config.py`, `orchestrator.py`, `router.py`): coordinate in
  commit messages; the second agent to touch a shared file must rebase first
- Do not edit the other agent's in-progress branch
- Both agents: run `python -m harness.pre_commit_check` before every commit
