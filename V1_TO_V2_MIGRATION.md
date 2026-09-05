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
| Narrow tool plane | `jarvis_v2/tools.py` | Owns a self-contained V2 schema and validator exposing only workspace file reads and read-only Git; no V1 registry import remains |
| Persistent local model server | `scripts/install_v2_local.py` | Starts one resident MLX model on `127.0.0.1:8080`, with prompt/decode concurrency and offline model loading |
| Deterministic tests | `tests/test_jarvis_v2_local_runtime.py` | Proves local-only URL enforcement, tool-loop behavior, checkpoints, malformed-call blocking, and path containment without a model download |
| Concurrent verified teams | `jarvis_v2/team.py` | Runs up to four local workers concurrently, records typed evidence digests, verifies assignment contracts, isolates failures, and synthesizes only verified results |
| Local benchmark harness | `scripts/benchmark_v2_concurrency.py` | Makes concurrency claims fail closed on worker, verifier, marker, synthesis, malformed-call, or overlap failures |
| Local pipeline observer | `scripts/v2_dashboard.py`, `scripts/v2_trace.py` | Shows checkpoint and sub-step timing while enforcing loopback/no-redirect probes, capability-gated APIs, exact worker labels, and metadata-only traces by default |
| V2 visual identity | `assets/v2/` | Original dark guardian icon with 1024px PNG, complete iconset, and validated macOS ICNS |

## Retained and upgraded from V1

- The V1 tool registry informed the bootstrap contract, but V2 now owns its
  minimal file/Git schemas and validation. V2 no longer imports the V1 registry.
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

The strict streaming 1/2/4 concurrent-worker gate passed on 2026-09-04 with
every worker and synthesis verified. End-to-end latency was 6.51, 9.85, and
17.40 seconds; peak concurrent in-flight model requests reached 1, 2, and 4.
Time to first delivered semantic delta ranged from 0.38 to 2.59 seconds across
worker turns. Acceptance required exact structured answer equality and one
bound tool/arguments/result call. This proves concurrent requests reached
MLX-LM, not simultaneous hardware decoding or raw first-decoder-token latency.

The V2 dashboard audit then found and closed misleading or unsafe observer
behavior: remote/redirect endpoint probing, symlinked state-root escape, raw
trace previews, creation-order worker labels, hardcoded run limits, and phantom
traces after readiness failure. A live three-worker trace completed with 23
records and distinct `git-observer`, `config-reader`, `model-reader`,
`synthesis`, and `team` actors. Its default trace contained no raw task, model,
argument, result, or error fields; digests and counts remained available.

The exact repository gate after the observer and V2 tool-plane changes passed:
3,850 passed, 8 skipped, 0 failed. Pytest also reported one non-fatal warning
from a retired-V1 `task_runtime.py` background thread whose task record had
already been cleared. That warning is retained as legacy evidence and is not a
V2 runtime thread.

The next gates are:

1. Run repeated soak, malicious-evidence, worker-crash, and verifier-failure trials.
2. Add owner-approved write tools behind digest-bound grants and verification.
3. Port memory/evidence, voice, chat, and files in that order.
4. Package a new `Jarvis V2.app` and verify it independently of the repo.
5. Run the full suite, local network-boundary audit, rollback drill, and
   migration-ledger reconciliation before calling V2 production-ready.

Deadline-aware in-flight cancellation is implemented at the loopback HTTP
socket boundary. Owner cancellation and wall-clock expiry now interrupt a
stalled request instead of waiting for the 120-180 second model timeout; they
checkpoint as `cancelled` and `blocked` respectively. Tests cover cancellation
before any SSE data, after a partial SSE frame, and at the agent deadline.
The resulting exact repository gate passed 3,855 tests with 8 skipped and no
failures.

The repository regression gate for the current observer checkpoint is green:
3,850 passed, 8 skipped, 0 failed. A migration-only test-reset repair prevents
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
