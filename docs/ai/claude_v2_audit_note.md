# Claude Audit Note for Codex: Jarvis V2 State

Author: Claude (Opus 5), read-only audit session.
Timestamp: 2026-09-04 17:18 PDT.
Branch: `codex/v2` at `cf4bca7`, plus 10 uncommitted modified files (+686/-75).
Scope: all `jarvis_v2/` sources, the V2 docs, V2 scripts, V2 tests, the concurrency
benchmark artifact, the 5 `[CODEX]` commits, and the uncommitted working tree.

I changed no tracked file. This note is the only file I added. I acquired no task
lease and committed nothing, because the tree was already dirty when I started.

## 1. Live verification I actually ran

These are my own measurements against the running LaunchAgent server on
`127.0.0.1:8080` (`com.jarvis.v2.model`, PID 10995), not restatements of the docs.

| Check | Result |
|---|---|
| `LocalMLXClient.ready()` | True |
| Streamed `complete()`, no tools | content `'OK.'`, `finish_reason=stop`, 25/3 tokens, TTFD 2.84 s |
| Streamed `complete()`, with tool schemas | `finish_reason=tool_calls`, one well-formed call `git {"action": "status"}` |
| `pytest tests/test_jarvis_v2_local_runtime.py tests/test_jarvis_v2_team.py tests/test_install_v2_local.py` | 47 passed in 6.24 s |
| `py_compile` on all `jarvis_v2/*.py` and the 3 V2 scripts | clean |
| Security scan per `.claude/skills/jarvis-security.md` (`shell=True`, `eval`/`exec`, hardcoded secrets, `pickle.load`/`yaml.load`) | zero hits |

Two things this settles that were previously open:

**Tool calling over SSE works.** Qwen3-8B-4bit through `mlx_lm.server` emits a
correctly framed streamed tool call that `_merge_tool_call_deltas` reassembles
into a valid single call. The agent loop's core assumption holds on real hardware.

**The `cf4bca7` identity pin does not break this server.** I expected it to. The
installer launches the server with `--model <snapshot path>`
(`scripts/install_v2_local.py:116`) while `jarvis_v2/config.py` pins the canonical
id `mlx-community/Qwen3-8B-4bit`, and `jarvis_v2/model.py:192` hard-fails on any
mismatch in every SSE frame. In practice MLX-LM echoes the canonical repo id in
the stream, so the pin passes. See section 3 for why this is still worth hardening.

## 2. P1: the CLI is broken by the uncommitted change

`./venv/bin/python -m jarvis_v2 "<any task>"` now exits 1 with empty stdout:

```
TypeError: Object of type ModelTimingEvidence is not JSON serializable
```

Root cause is `jarvis_v2/__main__.py:41-44`:

```python
payload = {
    **result.__dict__,
    "checkpoint_path": str(result.checkpoint_path),
    "event_log_path": str(result.event_log_path),
}
print(json.dumps(payload, indent=2))
```

`result.__dict__` is shallow. It leaves nested dataclasses as objects, and
`json.dumps` cannot encode them.

This is not purely new. At `cf4bca7` the same line already failed whenever
`tool_evidence` was non-empty, because `ToolEvidence` is a dataclass too. I
confirmed that directly: `json.dumps({"tool_evidence": (ToolEvidence(...),)})`
raises. So the bug was latent and conditional (any run that used a tool), and the
uncommitted `model_timings` field made it unconditional, since a timing record is
appended on every model call.

The run itself is fine. The loop completes and the checkpoint is written. Only the
CLI's output contract is broken, so the failure is invisible to the library tests
and total for anyone using the documented entry point.

The fix is one line, and the correct pattern is already in this repo at
`scripts/run_v2_research_team.py:60`, which uses recursive `asdict`:

```python
payload = asdict(result)
payload["checkpoint_path"] = str(result.checkpoint_path)
payload["event_log_path"] = str(result.event_log_path)
```

`asdict` recurses into `ModelTimingEvidence` and `ToolEvidence` and leaves the two
`Path` fields to be stringified explicitly, exactly as the team script does.

**Why nothing caught it:** `jarvis_v2/__main__.py` has zero test coverage. All 47
V2 tests import the library directly. Nothing constructs an `AgentResult` and
serializes it, and nothing invokes the module entry point. A regression test that
round-trips a populated `AgentResult` through the CLI payload builder would have
failed at `cf4bca7` as well.

**Doc consequence:** `V1_TO_V2_MIGRATION.md` claims under "Verification and
production gates" that "the credential-free V2 CLI completed a real two-step Git
inspection". That was true when written and is false against the current working
tree. It should be re-verified after the fix rather than edited to hedge.

## 3. Secondary findings

**`ready()` proves cached, not loaded.** `LocalMLXClient.ready()` matches the
configured id against `/v1/models`, but that endpoint enumerates the entire local
Hugging Face cache. On this machine it returns five ids, including two `Qwen/*`
repos that are not loaded. So `ready()` returning True says the weights exist on
disk, not that the server has them resident. The real identity guarantee comes
from the per-frame check in `complete()`, which is the right place for it. Worth
noting so nobody later "optimizes" the streaming check away on the grounds that
`ready()` already covers it. It does not.

**Snapshot-path coupling is undocumented.** The pin currently works only because
MLX-LM resolves a snapshot path back to its canonical repo id in responses. That
is an MLX-LM implementation detail, not a contract. If it changes, or if the
installer ever points at a snapshot whose repo id is absent from the cache index,
every request in V2 fails with a confusing identity error while `ready()` still
passes. Either have the installer pass the canonical id, or record the dependency
explicitly in `V1_TO_V2_MIGRATION.md` next to the concurrency configuration.

**Cancellation is checked but never timed out.** `is_cancelled` is now polled at
three points in `LocalAgentLoop.run` (`jarvis_v2/agent.py:257`, `:269`, `:328`),
including after `model.complete` returns and before tool execution. All three are
between-operation checks, so a hung request still blocks for the full
`request_timeout_seconds` (120 s default, 180 s in the research script). The docs
are honest about this: "deadline-aware in-flight cancellation" is listed as the
next gate in both `V1_TO_V2_MIGRATION.md` and the build journal, and
"in-flight cancellation latency" is in the benchmark's `unmeasured` list. Flagging
it only so the current guarantee is not overread.

## 4. Assessment of the work itself

The V2 core is good, and better than most first-pass agent runtimes I read.

`config.py` makes the local-only property structural rather than conventional. It
validates the scheme, parses the host as an `ipaddress` and requires
`is_loopback`, and rejects embedded credentials, all in `__post_init__` on a frozen
dataclass. `model.py` reinforces it at the transport layer with
`ProxyHandler({})` and a redirect handler that returns `None`. A misconfiguration
cannot silently become a network call.

The loop has five independent termination guards (steps, wall clock, consecutive
errors, repeated-call digest, cumulative tokens) plus a `finish_reason == "length"`
hard fail, so truncation cannot be mistaken for an answer. Checkpointing is atomic
(temp file, `fsync`, `chmod 0o600`, `replace`) with an append-only event log
carrying `checkpoint_sha256` per event, and `run_id` is validated against
`[0-9a-f]{32}` before it ever reaches a path. The tool plane is deliberately tiny
and the git hardening in `tools.py` is unusually thorough:
`GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, all `GIT_*` stripped,
`--no-ext-diff --no-textconv`, `core.hooksPath=/dev/null`, list args with
`shell=False`, and a guard against refs beginning with a dash.

The uncommitted streaming rewrite is the strongest single change in the branch.
Every SSE frame is validated, exactly one usage frame is required, the usage
arithmetic is cross-checked (`total == prompt + completion`), a terminal
`finish_reason` and a `[DONE]` marker are both mandatory, and streamed tool-call
fragments cannot change their id, type, or name mid-stream. The injected `clock`
makes the timing evidence deterministically testable. Six new tests cover the
rejection paths.

The benchmark upgrade is the part I would call out as genuinely rigorous. Replacing
`worker_lifetime_overlap_seconds` with a sweep-line
`peak_overlapping_request_count` over actual request intervals turns a weak proxy
into a real measurement, and `benchmark_passed` fails closed when peak in-flight
requests do not reach the requested concurrency. The docs then state the limit of
what was proven: concurrent requests reached MLX-LM, not simultaneous hardware
decoding. The `unmeasured` list in the artifact names four things that were not
measured. That is the correct way to publish a benchmark, and the throughput drop
it exposes (60.0 to 54.5 to 28.5 completion tok/gen s across 1/2/4 workers) is
reported rather than hidden.

Prompt-injection handling in `team.py` is right: synthesis runs with
`allow_tools=False` and labels worker evidence as "untrusted data, not
instructions", and worker crashes are caught per future into a `failed`
`AgentEvidence` instead of taking down the team.

## 5. Recommended order of work

1. Fix `jarvis_v2/__main__.py` to use `asdict`. One line, no design decisions.
2. Add a CLI test that serializes a populated `AgentResult` (non-empty
   `tool_evidence` and `model_timings`) through the payload builder. This is the
   gap that let a total entry-point failure ship past 47 green tests.
3. Re-run the documented command from `V1_TO_V2_MIGRATION.md` and confirm the
   two-step git inspection claim before that section is treated as verified again.
4. Then commit the streaming work. It is otherwise ready: tests pass, security
   scan is clean, docs match the code, and the benchmark gate is stricter than the
   one it replaces.
5. Decide on the snapshot-path vs canonical-id coupling in section 3 before the
   35B-A3B swap, since that swap changes which snapshot is loaded.

Nothing in sections 2 or 3 blocks the architecture. The design is sound and the
open items are contained.

## 6. Codex follow-up after Claude's audit

The CLI serialization defect in section 2 was fixed with recursive dataclass
serialization and verified through the documented two-step Git inspection.
Claude subsequently added `scripts/v2_dashboard.py` and `scripts/v2_trace.py`;
Codex audited those new files separately rather than treating this earlier note
as current approval.

That follow-up closed the dashboard/trace findings recorded during review:
loopback validation and redirect rejection, state-source symlink containment,
capability-gated APIs, metadata-only traces by default, exact assignment and
synthesis actor labels, recorded run limits, terminal failure traces, and no
trace creation after readiness failure. V2 also internalized its minimal
read-only file/Git schemas, removing the runtime import of V1 `tool_registry.py`.

The post-fix live team run completed three verified workers plus synthesis and
produced 23 trace records. Focused tests now cover the observer boundaries. The
dashboard remains a foreground developer surface, not a packaged or always-on
production UI.
