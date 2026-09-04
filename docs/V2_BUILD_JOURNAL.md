# Jarvis V2 Build and Findings Journal

This journal is the evidence source for future public updates about Jarvis V2.
Entries separate observed results from hypotheses and record failures and
limitations alongside successes.

## Product intent

Jarvis V2 is a local, owner-controlled ethical coworker for authorized AI
cybersecurity, Trust & Safety, and AI-safety operations. It is intended to help
inspect evidence, analyze code and incidents, maintain case context, execute
approved tools, and produce reviewable findings without sending model prompts
or private work data to a hosted inference provider.

Ethical coworker means:

- authorized scope is explicit before consequential action
- evidence and tool results remain distinguishable from model inference
- sensitive actions require owner approval and are logged
- uncertainty, failed checks, and missing data are surfaced
- the human remains the decision owner
- defensive security and safety work are the intended operating domain
- GitHub Copilot is not used for V2 development or evidence generation

## Public-claim rules

Future LinkedIn posts may claim only outcomes reproduced from this repository
or its saved benchmark artifacts. Every post should include:

1. Exact Apple Silicon hardware and memory.
2. Model name, quantization, runtime, and version.
3. Whether weights were already cached or downloaded.
4. Prompt/task definition and concurrency level.
5. Measured latency, throughput, memory, completion, and tool-call validity.
6. What failed, what remains untested, and what is not yet production-ready.
7. A clear distinction between local inference and Claude/Codex development help.

Do not repeat promotional claims such as “built an app in two minutes” or
“unlimited” unless they are independently reproduced with saved evidence.

## 2026-09-04 — V2 production foundation

### Hypothesis

One resident MLX-LM model on an M4 Pro can support a credential-free local
agent loop with typed tool calls, durable checkpoints, and bounded failure
behavior.

### Environment

- Mac: M4 Pro
- Unified memory: 48 GB
- MLX-LM: 0.31.3
- Bootstrap model: `mlx-community/Qwen3-8B-4bit`
- Model source: already present in the local Hugging Face cache
- Endpoint: `http://127.0.0.1:8080/v1`

### Verified before implementation

- A temporary MLX-LM server completed a two-turn model/tool/model exchange.
- The model emitted a structured tool call, accepted a synthetic tool result,
  and returned a correct final response.
- No API key or hosted model was used.

### Implemented

- Loopback-only configuration that rejects remote, LAN, hostname, HTTPS, and
  credential-bearing endpoints.
- Credential-free local HTTP model client.
- Bounded observe/act loop with step, wall-time, retry, malformed-call, and
  repeated-call controls.
- Atomic local run checkpoints and blocked-run resume.
- Append-only checkpoint evidence and explicit owner cancellation.
- Read-only workspace file and Git tools using the existing typed V1 schemas.
- Persistent MLX-LM LaunchAgent with offline model loading and prompt/decode
  concurrency.
- Deterministic tests that do not download or invoke a model.

### Live result after implementation

- The persistent LaunchAgent started successfully from local cached weights.
- `GET /v1/models` responded on loopback port 8080.
- A real V2 task inspected Git status and returned the correct changed-file set.
- The run completed in two model steps with checkpoint ID
  `94f4ebc3b7cb474c93e26e5ca549eb39`.
- The legacy app bundle and three V1 LaunchAgent files were removed; legacy
  ports 7842 and 8765 were closed.
- Focused V2 checks: 13 passed.
- Full repository regression suite: 3,802 passed, 8 skipped, 0 failed.

### Known limitations

- Qwen3-8B-4bit is the bootstrap model, not the final reasoning target.
- Current V2 tools are read-only.
- The coordinator implements bounded fan-out, typed evidence handoff,
  deterministic acceptance verification, and synthesis. Workers do not yet
  dynamically reassign work or hold peer-to-peer conversations.
- The client is non-streaming, so time to first token and decode throughput are
  not yet measured separately from end-to-end latency.
- Cancellation is checked between model turns; an in-flight local HTTP request
  can run until its bounded request timeout.
- Voice, memory, browser, case management, UI, and packaged-app parity remain
  migration work.
- Local inference removes provider quotas and remote policy layers, but does
  not remove finite memory, compute, model context, model behavior, or license
  constraints.

### 2026-09-04 — Concurrent team break/fix experiment

The first heterogeneous three-worker run exposed truncated worker and synthesis
answers. V2 now rejects `finish_reason=length`, disables thinking for bounded
tool tasks, prevents tools during synthesis, accumulates prompt/completion token
budgets, and records model usage. Additional adversarial review found and fixed:

- checkpoint races and duplicate event sequences
- concurrent ownership of one resumed run
- checkpoint path traversal through a tampered internal run ID
- inherited HTTP proxies and redirects that could move prompts off-device
- Git `fsmonitor` and `textconv` helpers that could execute repository config
- unsupported completion without real tool evidence
- unrelated-tool evidence satisfying the wrong assignment
- synthesis crashes being reported as team success
- owner-readable state and event files being created with broad permissions

The coordinator now records immutable tool-call evidence digests, verifies each
worker against a typed acceptance contract, passes only verified evidence to a
separate no-tools synthesizer, and requires every verification plus synthesis to
pass before the team is `completed`.

#### Strict 1/2/4 benchmark

Raw artifact: `.jarvis-v2/benchmarks/benchmark-1788522378.json` (local, ignored,
mode `0600`). The sanitized evidence record committed for review is
`docs/benchmarks/v2-concurrency-2026-09-04.json`. One resident
`mlx-community/Qwen3-8B-4bit` model served all runs.

| Workers | Verified | End-to-end | Worker-lifetime overlap | Prompt / completion tokens | Peak server RSS | Malformed calls |
|---:|---:|---:|---:|---:|---:|---:|
| 1 | 1/1 | 9.13 s | n/a | 2,205 / 284 | 255,803,392 B | 0% |
| 2 | 2/2 | 11.84 s | 6.81 s | 4,242 / 464 | 262,291,456 B | 0% |
| 4 | 4/4 | 21.99 s | 12.50 s | 8,284 / 861 | 277,807,104 B | 0% |

Each worker had to return JSON exactly equal to the trusted Git-status payload,
include its unique marker, execute exactly one call matching the bound Git tool,
canonical arguments digest, and trusted result digest, pass coordinator
verification, and complete team synthesis. The first attempted benchmark was
invalidated before commit when review found that independently checked markers
and digests allowed a marker-only answer. The structured-equality regression now
prevents that false positive. The benchmark exits nonzero for partial synthesis,
wrong content, missing markers, failed verification, malformed calls, or zero
worker-lifetime overlap above one worker.

#### Heterogeneous research team

Run `fb1467230a204d4c8700769e70bc04e4` assigned repository, runtime, and test
inspection to three specialists. All three used the required tool and passed
verification; their worker lifetimes overlapped for 30.57 seconds and completed synthesis in
45.00 seconds. The local synthesizer returned `Not Ready`, correctly identifying
that dynamic reassignment and deeper multi-agent tests remain future work.

### Next experiment

Add streaming telemetry and deadline-aware in-flight cancellation, then run
repeated soak and adversarial-evidence trials. Desktop packaging begins only
after those runtime gates and the full repository suite pass.

### Integration gate outcome

The V2 focused suite passes 37 tests. The first three mandatory complete-suite
runs exposed a frozen-V1 test-reset defect: a timed-out worker could become
untracked while retaining an agent lock or model semaphore, leaving the next
webhook task at `assigned`.

With explicit owner approval, `reset_for_tests()` was narrowly repaired to
replace those process-local execution primitives after clearing test state.
Normal V1 bootstrap and product execution are unchanged. A direct regression
holds both old primitives during reset and proves a new webhook-style task still
reaches `succeeded`. The final exact repository gate passed: 3,828 passed,
8 skipped, 0 failed.

### Visual identity transition

- Removed the stale Desktop V1 symlink and ignored V1 app build artifact.
- Generated a new original dark guardian mark for V2 rather than copying an
  existing film character or AI-company identity.
- Verified the master PNG is 1024×1024 with alpha transparency.
- Generated all standard macOS iconset sizes and validated
  `assets/v2/jarvis-v2.icns` with `iconutil` and `file`.
- The icon will not appear on the Desktop until a real V2 app package passes
  the packaging and runtime verification gate.

## LinkedIn draft scaffold

Use this only after substituting verified benchmark values:

> I started rebuilding Jarvis V2 as a fully local ethical coworker for AI
> cybersecurity and Trust & Safety work. On an M4 Pro with 48 GB unified
> memory, the current foundation runs [MODEL] through MLX-LM on loopback with
> no cloud inference or API keys. At [CONCURRENCY] concurrent tasks, it measured
> [RESULTS]. What worked: [VERIFIED]. What did not: [FAILURES]. Current limits:
> [LIMITATIONS]. Claude and Codex help develop the system, but the shipped
> inference and work data remain local.
