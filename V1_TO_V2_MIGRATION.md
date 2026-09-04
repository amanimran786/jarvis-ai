# Jarvis V1 to V2 Migration Ledger

This is the authoritative transition record for the retirement of Jarvis V1 and
the production initiation of Jarvis V2. It distinguishes what is frozen,
uninstalled, retained for audit, implemented, and still planned.

## Decision and boundary

- Decision date: 2026-09-04 (America/Los_Angeles).
- V1 feature development and update support ended on this date.
- V1 source is frozen at commit `598d0f106095fe718550baa8a43048b143a2ec33`.
- The annotated recovery tag is `jarvis-v1-final-2026-09-04`.
- V2 development occurs on `codex/v2` until its production gate passes.
- Claude and Codex may develop V2. Neither is a runtime dependency. Shipped
  inference remains local and independently operable.
- GitHub Copilot is excluded from V2 development by owner decision.

## Removed from the installed system

The V2 installer removes these V1 runtime surfaces when invoked with
`--remove-v1`:

| Removed surface | Previous purpose | V2 disposition |
|---|---|---|
| `~/Applications/Jarvis.app` | Packaged V1 PyQt desktop app | Removed; a V2 app will be packaged only after parity gates pass |
| `com.jarvis.loop` | Persistent V1 loop | Disabled, unloaded, plist removed |
| `com.jarvis.dashboard` | V1 dashboard daemon | Disabled, unloaded, plist removed |
| `ai.jarvis.overnight-training` | V1 overnight training | Disabled, unloaded, plist removed |
| Port `7842` dashboard | V1 status surface | No longer active |
| Port `8765` API | V1 API and orchestration surface | No longer active |
| `~/Desktop/Jarvis.app` | Symlink to installed V1 app | Removed after the target app was uninstalled |
| `dist/Jarvis.app` | Ignored V1 build artifact | Removed from the local checkout |

The Git tag and repository history are intentionally retained. Uninstalling a
runtime must not erase the evidence required to explain or recover it.

## Added in the V2 production foundation

| V2 capability | Implementation | Improvement over V1 |
|---|---|---|
| Strict-local model configuration | `jarvis_v2/config.py` | Rejects HTTPS, hostnames, credentials, LAN IPs, and all non-loopback endpoints |
| Credential-free MLX client | `jarvis_v2/model.py` | Uses localhost HTTP directly and sends no authorization or API-key field |
| Bounded agent loop | `jarvis_v2/agent.py` | Explicit step, time, retry, malformed-call, and no-progress limits |
| Durable checkpoints and event log | `jarvis_v2/agent.py` | Every run has an atomic checkpoint, append-only evidence log, owner cancellation, and blocked-run resume |
| Narrow tool plane | `jarvis_v2/tools.py` | Reuses typed V1 validation while initially exposing only workspace file reads and read-only Git |
| Persistent local model server | `scripts/install_v2_local.py` | Starts one resident MLX model on `127.0.0.1:8080`, with prompt/decode concurrency and offline model loading |
| Deterministic tests | `tests/test_jarvis_v2_local_runtime.py` | Proves local-only URL enforcement, tool-loop behavior, checkpoints, malformed-call blocking, and path containment without a model download |
| Concurrent verified teams | `jarvis_v2/team.py` | Runs up to four local workers concurrently, records typed evidence digests, verifies assignment contracts, isolates failures, and synthesizes only verified results |
| Local benchmark harness | `scripts/benchmark_v2_concurrency.py` | Makes concurrency claims fail closed on worker, verifier, marker, synthesis, malformed-call, or overlap failures |
| V2 visual identity | `assets/v2/` | Original dark guardian icon with 1024px PNG, complete iconset, and validated macOS ICNS |

## Retained and upgraded from V1

- `tool_registry.ToolSpec` and `tool_registry.validate_args` remain the typed
  schema boundary. V2 exposes a smaller allowlist during bootstrap.
- V1 execution evidence and approval concepts remain design inputs. V2 makes
  loop state and checkpoints first-class instead of depending on implicit
  orchestration state.
- Existing Ollama-hosted `nomic-embed-text` remains the planned local embedding
  service because MLX-LM's chat server is not an embedding endpoint.
- V1 tests remain in the repository as regression and migration evidence until
  each retained capability is either ported or explicitly retired.

## Model and concurrency configuration

The currently installed bootstrap model is the already-cached
`mlx-community/Qwen3-8B-4bit`. It fits comfortably on the M4 Pro with 48 GB
unified memory and proves the architecture without downloading a model.

The service is configured for:

- loopback only: `127.0.0.1:8080`
- offline Hugging Face/Transformers mode
- four concurrent decode slots
- two concurrent prompt-prefill slots
- eight prompt-cache entries
- deterministic default temperature `0.0`

The 8B model is a bootstrap, not the final V2 reasoning target. A 35B-A3B
4-bit model will replace it only after a measured 1/2/4-request benchmark shows
acceptable memory pressure, time-to-first-token, decode speed, tool-call
validity, and completion rate on this exact Mac.

## Local ownership and remaining limits

V2 removes hosted-provider accounts, API keys, usage quotas, remote moderation
layers, and per-token billing from its runtime path. The owner controls the
model, prompts, tools, data, permissions, and update cadence.

Local ownership does not remove physical or legal constraints. V2 still has
finite memory and compute, model context limits, open-weight license terms, and
explicit local tool-permission boundaries. These boundaries are visible and
owner-configurable; they are not imposed by a cloud inference provider.

## Verification and production gates

Production has been initiated, but feature parity has not yet been declared.
On 2026-09-04, the V2 LaunchAgent was verified running on loopback, the
credential-free V2 CLI completed a real two-step Git inspection, the V1 app and
legacy plists were confirmed absent, legacy ports were confirmed closed, and
the repository suite passed with 3,802 tests (8 skipped).

The strict 1/2/4 concurrent-worker gate passed on 2026-09-04 with every worker
and synthesis verified. End-to-end latency was 9.13, 11.84, and 21.99 seconds;
two/four-worker lifetime overlap was 6.81/12.50 seconds; malformed tool-call rate
was zero. Acceptance required exact structured answer equality and one bound
tool/arguments/result call. The artifact and limitations are in the build journal.

The next gates are:

1. Add streaming/TTFT telemetry and deadline-aware in-flight cancellation.
2. Run repeated soak, malicious-evidence, worker-crash, and verifier-failure trials.
3. Add owner-approved write tools behind digest-bound grants and verification.
4. Port memory/evidence, voice, chat, and files in that order.
5. Package a new `Jarvis V2.app` and verify it independently of the repo.
6. Run the full suite, local network-boundary audit, rollback drill, and
   migration-ledger reconciliation before calling V2 production-ready.

The repository regression gate for this concurrency checkpoint is green:
3,828 passed, 8 skipped, 0 failed. A migration-only test-reset repair prevents
retired V1 workers from leaking process-local locks across tests; normal V1
runtime behavior was not changed.

## Commands

Install V2 and remove installed V1 surfaces:

```bash
./venv/bin/python scripts/install_v2_local.py --remove-v1
```

Run a local inspection task:

```bash
./venv/bin/python -m jarvis_v2 \
  "Inspect git status and explain what needs attention" \
  --workspace /Users/truthseeker/jarvis-ai
```

No API key is accepted or required by either command.

Public development findings and future LinkedIn evidence are maintained in
[`docs/V2_BUILD_JOURNAL.md`](docs/V2_BUILD_JOURNAL.md).
The reusable clean-Mac installation procedure is in
[`docs/V2_LOCAL_AGENTS_FROM_SCRATCH.md`](docs/V2_LOCAL_AGENTS_FROM_SCRATCH.md).
