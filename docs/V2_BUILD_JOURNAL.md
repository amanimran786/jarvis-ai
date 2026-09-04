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
- Live 1/2/4-request concurrency has not yet been benchmarked through V2.
- Current V2 tools are read-only.
- Voice, memory, browser, case management, UI, and packaged-app parity remain
  migration work.
- Local inference removes provider quotas and remote policy layers, but does
  not remove finite memory, compute, model context, model behavior, or license
  constraints.

### Next experiment

Run the same read-only evidence task at concurrency 1, 2, and 4 against one
resident model. Save raw timings and responses, then report TTFT, total latency,
decode throughput, peak memory, success rate, and malformed-tool-call rate.

## LinkedIn draft scaffold

Use this only after substituting verified benchmark values:

> I started rebuilding Jarvis V2 as a fully local ethical coworker for AI
> cybersecurity and Trust & Safety work. On an M4 Pro with 48 GB unified
> memory, the current foundation runs [MODEL] through MLX-LM on loopback with
> no cloud inference or API keys. At [CONCURRENCY] concurrent tasks, it measured
> [RESULTS]. What worked: [VERIFIED]. What did not: [FAILURES]. Current limits:
> [LIMITATIONS]. Claude and Codex help develop the system, but the shipped
> inference and work data remain local.
