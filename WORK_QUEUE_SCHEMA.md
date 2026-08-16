# WORK_QUEUE.json Coordination V2 Schema

`WORK_QUEUE.json` is the durable engineering ledger. Codex is the sole control
plane; workers interact with it only through `harness.agent_coordinator`.
Direct status edits and worker-driven queue selection are unsupported.

## Typed Task

Codex authors the task and its matching entry in `TASK_CONTRACTS.json` before
assignment:

```json
{
  "id": "TASK-042",
  "title": "Add rate limiting to web_fetch",
  "description": "Retry transient fetch failures and surface final errors.",
  "goal": "Make web fetching resilient without hiding failures.",
  "allowed_files": [
    "harness/web_search.py",
    "tests/test_web_search.py"
  ],
  "forbidden_files": [".env", "config/credentials.json"],
  "acceptance_criteria": [
    "Retries transient failures with bounded exponential backoff",
    "Raises WebFetchError after the retry budget is exhausted",
    "Focused and full test suites pass"
  ],
  "verification_commands": [
    "python -m pytest tests/test_web_search.py -q"
  ],
  "constraints": {
    "local_first": true,
    "network": false,
    "poc_required": false
  },
  "budget": {
    "max_attempts": 3,
    "wall_time_seconds": 1800,
    "tool_calls": 40
  },
  "domain": "harness",
  "assigned_ai": "claude",
  "priority": 1,
  "status": "proposed",
  "created_at": "2026-08-15T00:00:00Z"
}
```

Modern executable task rows require explicit `assigned_ai`. It is a normalized
planning hint retained in the task digest; `worker_type` in the Codex assignment
is the actual execution identity. A proposal may leave `assigned_ai` null, in
which case `agent_coordinator assign` binds it to the selected worker before
validating the contract. Valid values are `claude` and `codex`; `local` remains
accepted only when `constraints.isolated_runtime` is explicitly `true`.
Unsupported providers are rejected. Old completed rows keep their original
values for audit history.

## Codex Assignment Metadata

`agent_coordinator assign` adds immutable assignment bindings:

```json
{
  "status": "queued",
  "assigned_to": "claude",
  "worker_type": "claude",
  "orchestrated_by": "codex",
  "orchestration_id": "orch_0123456789abcdef",
  "orchestration_stage": "implementation",
  "orchestration_state": "assigned",
  "orchestration_rationale": "Highest-priority verified roadmap gap",
  "orchestration_assigned_at": "2026-08-15T00:05:00Z",
  "orchestration_contract_sha256": "<sha256>",
  "orchestration_task_spec_sha256": "<sha256>",
  "orchestration_base_ref": "<git commit>",
  "coordination_version": 2
}
```

Allowed orchestration stages are `poc`, `implementation`, `hardening`, and
`release`. An implementation that requires an accepted POC also stores
`orchestration_parent_task_id` and `orchestration_parent_poc_sha256`.

The assignment is invalid when its worker, task, contract, base commit, or POC
binding changes before claim.

## Status Lifecycle

```text
proposed
   |
   v (Codex selects and assigns)
queued / awaiting_approval
   |
   v (exact worker claims)
in_progress
   |
   v (deterministic verification passes)
awaiting_codex_review
   |                 |
   | accept          | reject
   v                 v
done             needs_review
```

| Status | Meaning |
|---|---|
| `proposed` | Non-executable task or POC suggestion awaiting Codex selection. |
| `queued` | Digest-bound Codex assignment waiting for its exact worker. |
| `awaiting_approval` | Assignment also requires independent human side-effect approval. |
| `in_progress` | Exact worker holds a valid lease. |
| `awaiting_codex_review` | Verification passed; Codex has not accepted the result. |
| `needs_review` | Codex rejected the candidate or verification needs intervention. |
| `unverified` | Evidence is incomplete or a legacy active row was quarantined. |
| `blocked` | A contract, approval, digest, or dependency precondition failed. |
| `done` | Deterministically verified and accepted by Codex. |
| `cancelled` | Intentionally retained as non-executable audit history. |

Only Codex review writes `completed_by: "codex"`. The worker identity is stored
separately as `executed_by`.

## POC And Human Approval

POC approval is an engineering-governance decision recorded by Codex after a
POC task passes verification and review. Its digest binds downstream work to the
accepted proof.

`requires_approval` in `TASK_CONTRACTS.json` is a separate human authorization
for risky side effects. A POC approval cannot satisfy that safety gate, and a
human side-effect approval cannot approve an architecture or POC.

## Local Model Proposals

`orchestrator_loop.py` may ask a local model for follow-up ideas. Every result
must be stored as:

```json
{
  "status": "proposed",
  "proposed_by": "local",
  "requires_codex_assignment": true,
  "assigned_ai": null,
  "assigned_to": null
}
```

Proposal generation cannot choose a worker, create a lease, launch execution,
or mark work complete.

## Completion Evidence

Worker summaries are context, not proof. `finish` collects loop-owned evidence:

```json
{
  "observer": "loop",
  "changed_files": ["harness/web_search.py", "tests/test_web_search.py"],
  "commands": [
    {
      "command": "python -m pytest tests/test_web_search.py -q",
      "exit_code": 0
    }
  ],
  "policy_findings": []
}
```

The verifier enforces allowed paths, required commands, policy findings, and a
clean committed checkout. Passing evidence produces a candidate for Codex
review; it does not by itself produce `done`.

## Legacy Migration

- Preserve terminal v1 rows without rewriting historical digests.
- Quarantine active rows that have no valid owner, lease ID, or expiry.
- Redefine nonterminal provider-named work under neutral v2 task IDs.
- Do not allow a v1 row to execute through a v2 claim.
- Archive terminal `LAUNCH_QUEUE.json` records before enabling a replacement
  transport.
