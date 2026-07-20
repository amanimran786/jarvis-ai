# Jarvis Architecture Review

Review date: 2026-07-16 PDT  
Reviewed revision: `f8d50042ed28740d3c8cc386119506e12b13434d`  
Scope: local-first policy, model routing, task execution, cross-agent coordination,
memory, event buses, API security, packaging, voice, and verification.

## Executive assessment

Jarvis has substantial production-oriented infrastructure: typed task contracts,
digest-bound approvals, atomic coordinator leases, verifier-owned completion,
fail-closed primary API authentication, bounded context assembly, and a large test
suite. The main risk is not missing components. It is that old and new components
remain active at the same time with incompatible ownership and policy rules.

The current source tree should not be treated as release-ready. Four issues are
release blockers:

1. Local-first behavior is not enforced at one boundary; several paths can still
   invoke cloud providers without an explicit per-request decision.
2. The container event bus is published without general endpoint authentication.
3. The macOS bundle can embed personal memory and operational state from the
   developer checkout.
4. Conversation memory has no provider-aware privacy boundary before cloud routing.

The installed app was last modified on 2026-07-12 while the reviewed source HEAD is
from 2026-07-16. Packaged behavior is therefore not assumed to match source behavior.

## Confirmed defects

### P0-1: Local-first policy is fragmented and fails open

**Evidence**

- The source-of-truth default is `auto`, not `open-source`
  (`config.py:288-290`).
- Mobile requests force GPT-mini and use direct GPT-mini as their fallback
  (`model_router.py:80-97`, `model_router.py:930-948`, `api.py:789-817`).
- The mobile override only affects `model_router`; intent classification can still
  call Claude Haiku after a local classification failure
  (`orchestrator.py:148-179`).
- Smart Listen reads `openai_fallback_allowed`, but when local STT is available and
  transcription fails it continues to OpenAI without checking that flag
  (`meeting_listener.py:594-627`).
- STT mode detection itself returns `True` on an exception, which is a privacy
  fail-open (`local_runtime/local_stt.py:87-94`).

**Impact**

Queries or meeting audio can leave the machine even when the operator expects the
local-first policy to prevent that. The full test run also observed an unintended
Anthropic classifier call on a mobile-web path.

**Concrete fix**

Create one immutable request policy carrying `local_only`, allowed providers, data
sensitivity, and explicit-cloud consent. Resolve it before classification, memory
retrieval, STT, and response routing. Default `DEFAULT_MODE` to `open-source`; make
all policy-resolution failures deny cloud use.

### P0-2: The container event bus accepts unauthenticated task traffic

**Evidence**

- Docker publishes Redis and the event bus on host interfaces
  (`docker-compose.yml:24-35`, `docker-compose.yml:81-96`).
- Task creation, result submission, task status, pending approvals, metrics, and
  agent inbox delivery have no shared authentication dependency
  (`infra/event_bus.py:359-437`, `infra/event_bus.py:534-587`).
- Only approval decisions have a dedicated token check
  (`infra/event_bus.py:451-530`).

**Impact**

Any client that can reach port 8766 can inject work, forge results, inspect task or
approval data, and consume an agent inbox. Direct Redis access on port 6379 expands
the same trust boundary.

**Concrete fix**

Bind Redis and the event bus to loopback by default, require Redis authentication,
and add service authentication plus per-agent authorization to every non-health
endpoint. Treat request origin metadata as untrusted input, not authentication.

### P0-3: Packaging can disclose private runtime state

**Evidence**

- `Jarvis.spec` recursively packages every repository file not matched by a
  denylist (`Jarvis.spec:11-64`).
- `memory.json`, snapshots, queue files, approval files, and usage JSONL files are
  not comprehensively excluded (`Jarvis.spec:38-45`).
- The installed 1.2 GB bundle was observed to contain `memory.json`, four snapshot
  copies of it, `usage_log.jsonl`, `approved_tasks.json`, and `WORK_QUEUE.json`.

**Impact**

A distributable application can contain conversation memory, approval state, and
engineering history from the build machine. Denylist packaging is also
non-reproducible because unrelated repository artifacts alter the bundle.

**Concrete fix**

Replace `iter_datas()` with an explicit allowlist of immutable runtime assets. Add a
post-build manifest test that rejects memory, logs, queues, approvals, credentials,
snapshots, databases, and temporary files anywhere in the bundle.

### P0-4: Provider selection occurs after private memory assembly

**Evidence**

- Verbatim conversation turns are indexed without redaction and labeled
  `semi_private` (`semantic_memory.py:156-188`).
- Retrieval includes all tiers when no tier filter is supplied
  (`semantic_memory.py:373-387`).
- Semantic context is assembled before the route plan chooses a provider
  (`model_router.py:990-1020`, `model_router.py:1130-1148`,
  `model_router.py:1174-1224`).

**Impact**

When a request ultimately routes to a cloud provider, the prompt can include
verbatim prior conversation even though retrieval did not make a provider-aware
privacy decision.

**Concrete fix**

Choose the provider class before retrieval. Cloud requests may retrieve only an
explicit `cloud_safe` tier after redaction; private and semi-private records should
remain local unless the user grants scoped consent for that request.

### P1-1: Two queue executors violate the single-checkout lease invariant

**Evidence**

- The coordinator deliberately permits one shared-checkout engineering lease
  (`harness/agent_coordinator.py:1-5`, `harness/agent_coordinator.py:420-455`).
- The legacy loop independently counts `SessionTracker` sessions and launches up to
  its own `max_concurrent`, which defaults to three
  (`orchestrator_loop.py:361-367`, `orchestrator_loop.py:1048-1052`).
- Missing capabilities in that loop are logged but do not block dispatch
  (`orchestrator_loop.py:432-440`).

**Impact**

The two controllers can make conflicting decisions over the same queue and shared
checkout. This reintroduces concurrent edits, duplicate dispatch, and consumed
approvals outside the coordinator's lease model.

**Concrete fix**

Make `harness.agent_coordinator` the only queue state-transition owner. The legacy
loop should request leases from it and launch only isolated worktrees when parallel
execution is enabled. Capability checks must be claim preconditions.

### P1-2: Queue identity and verifier recovery are split-brain

**Evidence**

- Coordinator identity prefers `contract_id`
  (`harness/agent_coordinator.py:117-120`).
- Legacy loop identity reparses a `TaskSpec` and can synthesize a `LEGACY-*` ID
  instead (`orchestrator_loop.py:668-676`).
- Legacy transitions compare only that derived identity
  (`orchestrator_loop.py:689-748`).
- On failed verification, the coordinator clears all lease provenance before
  storing `unverified` or `blocked` (`harness/agent_coordinator.py:54-63`,
  `harness/agent_coordinator.py:782-795`).
- The coordinator CLI has no retry or reverify operation
  (`harness/agent_coordinator.py:850-900`).

**Impact**

Contract-bound legacy rows can be unfindable by one controller. A transient verifier
infrastructure failure can strand an otherwise valid commit after its base ref and
contract evidence have been erased. This occurred during the preceding CLI history
task and required private helper calls to recover.

**Concrete fix**

Persist one canonical `task_id` during queue migration and reject rows without it.
Retain immutable attempt provenance after failure, and add public `retry` and
`reverify` commands that are atomic, audited, and test-covered.

### P1-3: Task-runtime leases can expire during valid execution

**Evidence**

- `lease_expires_at` is set once when a task is submitted
  (`task_runtime.py:2540-2559`).
- No worker heartbeat renews it while the task runs.
- The watchdog force-fails active tasks after expiry
  (`task_runtime.py:2436-2460`).
- Worker status updates and completion do not fence against an existing terminal
  state (`task_runtime.py:808-816`, `task_runtime.py:825-837`,
  `task_runtime.py:2111-2134`).

**Impact**

A long but healthy task can be marked failed while its worker continues, after which
the worker can overwrite that failure with `running` or `succeeded`.

**Concrete fix**

Renew leases from the active worker and make every transition compare-and-set from
an allowed prior state plus attempt ID. A terminal state must be immutable without a
new attempt.

### P1-4: Execution errors can verify as successful tool steps

**Evidence**

- Terminal and file helpers return `(True, result)` regardless of helper outcome
  (`execution_engine.py:148-155`, `execution_engine.py:172-177`).
- `terminal.run_command()` encodes denial, blocked commands, timeout, and nonzero
  execution as strings (`terminal.py:32-45`).
- The corresponding verifiers accept any nonempty string
  (`execution_engine.py:51-57`).

**Impact**

A denied command, failed process, or file error can satisfy a task step and allow the
agent loop to report success without producing the intended side effect.

**Concrete fix**

Return a typed `ToolResult` with `ok`, exit code, error class, stdout, stderr, and
side-effect evidence. Verifiers must inspect structured status and task-specific
postconditions, never string non-emptiness alone.

### P1-5: Operative recovery cannot turn a failed plan into success

**Evidence**

- Failed steps remain in the plan while corrective steps are appended
  (`operative.py:179-213`).
- Final task success depends on all steps being successful
  (`operative.py:211-252`).
- Corrective step numbering starts immediately after the failed step, which can
  collide with later original steps (`task_planner.py:233-240`).

**Impact**

The write-test-fix loop can execute a useful repair but still finish failed, and
colliding step IDs can corrupt result references and checkpoints.

**Concrete fix**

Represent each logical step separately from its attempts. A successful corrective
attempt should resolve the failed logical step; use globally unique attempt IDs and
compute the final verdict from resolved logical steps plus explicit postconditions.

### P1-6: Ollama Cloud fails on a memory API signature mismatch

**Evidence**

- Ollama Cloud calls `mem.get_context(user_input)`
  (`brains/brain_ollama.py:1730-1738`).
- `memory.get_context()` accepts no positional argument (`memory.py:277-283`).

**Impact**

Any Ollama Cloud call with context tracking enabled raises before opening the model
request. The full suite observed this exact `TypeError` in a fallback path.

**Concrete fix**

Call `get_context()` with no argument or define and test a query-aware context API.
Add a focused Ollama Cloud test with `track_context=True` and a mocked OpenAI-compatible
client.

### P1-7: Memory updates are atomic writes but not atomic transactions

**Evidence**

- `load()` and `save()` each take a thread lock independently
  (`memory.py:116-143`).
- Mutations perform read-modify-write outside one lock
  (`memory.py:148-178`).
- There is no interprocess lock, while GUI, API, console, and daemons can coexist.
- Semantic index invalidation is process-local state only
  (`semantic_memory.py:306-319`).

**Impact**

Concurrent writers can lose facts or preferences, and another process can serve a
stale semantic index indefinitely.

**Concrete fix**

Use SQLite WAL with transactional mutations, revisions, and one canonical record
schema. Make semantic and vector stores rebuildable projections keyed by the
canonical revision.

### P1-8: Event-bus delivery is not durable across backends

**Evidence**

- Redis inbox messages are acknowledged as soon as they are yielded, before task
  execution succeeds (`infra/event_bus.py:550-587`).
- Pending approvals are listed with `XRANGE`, so acknowledging a stream entry does
  not remove it from that listing (`infra/event_bus.py:432-448`,
  `infra/event_bus.py:530-531`).
- SQLite claims a durable task and then puts delivery only into an in-memory queue
  (`infra/event_bus_sqlite.py:100-115`).
- SQLite approval always changes an approved row to `queued` without validating the
  prior state (`infra/event_bus_sqlite.py:271-289`).

**Impact**

Tasks can disappear after a consumer crash, resolved approvals can remain visible,
and an invalid late approval can requeue completed work.

**Concrete fix**

Define one backend-neutral state machine: queued, leased, executing, awaiting
approval, succeeded, failed, with attempt IDs, lease expiry, idempotency keys, and
acknowledgment only after durable completion.

### P1-9: Audit logging stores prompt content without redaction

**Evidence**

- Router audit logs the first 500 characters of every query
  (`router.py:3197-3200`).
- Streaming API audit logs another 200 characters
  (`api.py:871-885`).
- The audit writer serializes arbitrary payload fields directly
  (`harness/audit.py:245-277`).
- The observed `logs/audit.jsonl` mode was `0644`.

**Impact**

Credentials, incident data, or personal content pasted into Jarvis can persist in a
plaintext file readable by other local accounts.

**Concrete fix**

Centralize structured redaction before all logging, omit prompt bodies by default,
record hashes or bounded classifications instead, and create audit/state files with
mode `0600`.

### P1-10: Completion verification is policy filtering, not containment

**Evidence**

- The verifier accepts `pytest`, `ruff`, and `compileall` module commands
  (`harness/completion_verifier.py:478-505`).
- Commands run directly on the host through `subprocess.Popen`
  (`harness/completion_verifier.py:173-188`).
- The emitted policy explicitly declares `full_sandbox: False`
  (`harness/completion_verifier.py:621-636`).

**Impact**

An allowed test module can still read home-directory files, access the network, or
mutate files outside Git while returning success. Command syntax validation does not
contain code executed by the command.

**Concrete fix**

Run completion checks in an isolated worktree inside a sandbox with explicit
filesystem and network policy. Preserve the current argv validation as defense in
depth.

### P1-11: Packaged Kokoro TTS depends on the developer checkout

**Evidence**

- The worker resolver searches absolute paths under
  `/Users/truthseeker/jarvis-ai` (`local_runtime/local_kokoro_subprocess_tts.py:65-100`).
- Python source files are excluded from bundle data (`Jarvis.spec:28-37`), and the
  installed bundle was observed without `local_runtime/tts_subprocess.py`.

**Impact**

The packaged TTS path works only while Aman's source checkout and virtual environment
remain at the expected absolute paths. Moving the app to another Mac breaks the
worker.

**Concrete fix**

Package a supported worker runtime and its assets under `Contents/Resources`, resolve
it relative to the bundle, and add a portability smoke test that runs with the source
checkout temporarily unavailable.

### P1-12: Console mode starts duplicate runtime services

**Evidence**

- GUI startup automatically opens a console process (`main.py:492-504`).
- `_run()` starts the API daemon, watcher, brain daemon, task runtime, heartbeat, and
  deferred tasks before checking `--console` (`main.py:507-530`).

**Impact**

GUI and console processes can run duplicate watchers, heartbeats, task-runtime
instances, and state writers. This amplifies memory races and embedded database
contention.

**Concrete fix**

Branch on `--console` before starting services. Make the console a client of the
already-running authenticated local API, with an explicit standalone-server mode for
headless operation.

### P2-1: The public pending page exposes source diffs

**Evidence**

- `/pending` renders up to 2,000 characters of a pending code diff
  (`api.py:7118-7144`).
- It is explicitly added to the public path set (`api.py:7216-7219`).
- The API accepts its configured tunnel hostname (`api.py:190-205`).

**Impact**

Anyone who discovers a reachable tunnel URL can inspect pending source changes
without authentication. Approve and reject remain protected, which limits but does
not remove the disclosure.

**Concrete fix**

Require authentication for diff content. A public page, if retained, should expose
only a non-sensitive count and pairing flow.

### P2-2: Classifier timeout does not cancel inference

**Evidence**

- Each request creates a new one-worker executor and waits for three seconds
  (`orchestrator.py:433-445`).
- Timeout calls `shutdown(wait=False)` without cancelling the running model request
  (`orchestrator.py:446-453`).
- `_FALLBACK` is a shared mutable `ToolDecision`; `_attach_skill()` can mutate its
  params across requests (`orchestrator.py:99`, `orchestrator.py:175-189`).

**Impact**

Repeated local classifier timeouts can accumulate background inference calls, and a
skill attached to the shared fallback can leak into a later unrelated decision.

**Concrete fix**

Use one bounded classifier worker with client-level cancellation. Return a fresh
fallback decision for every request.

## Recommendations, not confirmed defects

These changes improve maintainability but are not presented as current behavioral
failures:

1. **Retire duplicate orchestration stacks.** Keep one queue, lease, retry, approval,
   and completion state machine. Adapt CLI, dashboard, Cowork, and in-app task runtime
   to that service instead of maintaining independent transitions.
2. **Define one canonical memory model.** Store records, privacy tier, provenance,
   retention, and revision in one transactional database. Treat JSON, Mem0, Qdrant,
   and embeddings as migrations or projections, not equal sources of truth.
3. **Split the API by domain.** `api.py` is 8,094 lines and combines authentication,
   chat, mobile UI, dashboards, approvals, webhooks, and remote-control surfaces.
   Move route groups behind service interfaces while preserving the global auth
   middleware and request policy.
4. **Generate the packaging manifest.** Derive hidden imports and immutable assets
   from explicit module/resource ownership rather than a repository scan plus a long
   manual list (`Jarvis.spec:87-237`).
5. **Add real packaged acceptance tests.** Validate bundle privacy, first-run startup,
   local-only chat, microphone open/listen/close/reopen, Kokoro portability, and API
   authentication against `/Users/truthseeker/Applications/Jarvis.app`.
6. **Add rotating structured logs.** Voice and crash logging currently append directly
   (`voice.py:124`, `main.py:157`). Use size-based rotation, retention, component IDs,
   and run IDs.

## Dead-code and drift assessment

No material unreachable production path is asserted from this audit without dynamic
coverage evidence. One clear drift signal is the mobile comment claiming direct
Claude Haiku routing (`api.py:153-156`) while the implementation routes through the
full router and forces GPT-mini (`api.py:789-817`). Remove stale comments as part of
the policy fix. A future dead-code pass should use import graphs and runtime coverage,
not text search alone.

## Architecture strengths

- Coordinator claims are atomic, require a clean checkout, bind contracts and task
  specs by digest, and leave completion to loop-owned evidence
  (`harness/agent_coordinator.py:420-520`, `harness/agent_coordinator.py:708-810`).
- Approval records bind both contract and normalized task-spec digests and use atomic
  updates (`harness/approval_workflow.py:194-240`).
- Primary API authentication fails closed when no token is configured
  (`api.py:215-245`, `api.py:469-478`).
- Verifier commands reject shell syntax, run with `shell=False`, enforce timeouts,
  limit captured output, and terminate process groups
  (`harness/completion_verifier.py:173-188`,
  `harness/completion_verifier.py:478-505`).
- Frozen runtime writes are redirected to Application Support rather than mutating
  the app bundle (`runtime_state.py:108-139`).
- Semantic entry writes use unique temporary files and atomic replacement; query
  vectors and indexes are bounded and cached (`semantic_memory.py:157-188`,
  `semantic_memory.py:222-327`, `semantic_memory.py:470`).
- SQLite task persistence enables WAL and conditionally claims tasks in a transaction
  (`task_persistence.py:39`, `task_persistence.py:297`).

## Priority sequence

1. Enforce one local-first request policy across classification, memory, STT, and
   model routing.
2. Close event-bus network exposure and authenticate all task traffic.
3. Replace bundle denylisting with an allowlist and rebuild the installed app.
4. Add provider-aware memory privacy filtering.
5. Consolidate queue ownership under `agent_coordinator` and repair canonical task
   identity plus reverify support.
6. Correct tool-result and operative recovery semantics, then add regression tests.
7. Move memory to transactional ownership and make the console a pure API client.
8. Sandbox completion verification and expand packaged acceptance coverage.

## Verification notes

- Full repository suite before this report: 3,457 passed, 3 skipped, 34 subtests
  passed. One earlier run exposed the Anthropic mobile-classifier call and Ollama
  Cloud `get_context()` signature error; the immediate retry passed.
- Focused security and contract review: 98 tests passed.
- No implementation file was modified for this architecture-review contract.
- The installed app was inspected but not rebuilt; packaged fixes remain unverified.
