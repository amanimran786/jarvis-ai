# Local authentication log triage

V2 detects authentication failure bursts and a successful login following those
failures. Analysis uses the Python standard library on your Mac. Optional Qwen
summarization uses the existing loopback MLX service. No API key, cloud inference,
external package installation, or network target is needed.

## Try the synthetic incident

From the repository root:

```bash
./venv/bin/python scripts/run_v2_auth_triage.py tests/fixtures/v2_auth/suspicious.jsonl
```

Expected result: six events, five failures, one success, two alerts.
`AUTH-FAILURE-BURST` cites lines 1–5; `AUTH-SUCCESS-AFTER-FAILURES` cites
lines 1–6. A successful CLI exit means analysis completed, not that there are
no alerts.

Add a local model summary:

```bash
./venv/bin/python scripts/run_v2_auth_triage.py tests/fixtures/v2_auth/suspicious.jsonl --summarize
```

The summarizer receives the structured report and has no tools. Its counts and
exact rule/line citations must match the deterministic evidence. Assessment and
recommendations still need analyst review. A bad summary or unavailable model
gives status `partial` and exit 2 while preserving the analysis. Invalid input
gives status `error` and exit 2.

Inspect your own prepared export:

```bash
./venv/bin/python scripts/run_v2_auth_triage.py auth.jsonl --workspace /absolute/path/to/your/case
```

The explicit CLI invocation authorizes read-only analysis under a five-minute
grant. It does not provide an authenticated multi-user broker. Users with local
filesystem access can already invoke Python; user identity enforcement and
deployment isolation remain separate work.

## Input contract

Each nonblank UTF-8 JSONL line must have these fields:

```json
{"event_id":"unique-001","timestamp":"2026-09-06T10:00:00Z","event_type":"authentication","outcome":"failure","user":"example-user","host":"example-host","source_ip":"192.0.2.10"}
```

- `event_id`: unique non-empty string within the input.
- `timestamp`: ISO 8601 string with explicit timezone; normalized to UTC.
- `event_type`: exactly `authentication`.
- `outcome`: exactly `success` or `failure`.
- `user`, `host`: non-empty strings, matched exactly.
- `source_ip`: literal IPv4 or IPv6. No DNS lookup occurs.
- Additional fields such as `message` are ignored and not sent to the model.

This is a normalized export format. Raw EVTX, syslog, SIEM responses, and
vendor-specific JSON are not yet supported. Malformed rows and duplicate
event IDs/JSON keys reject the entire file instead of silently losing evidence.

## Detection semantics and limits

The detector correlates the same user, host, and source address. Five failures
within an inclusive 300-second sliding window produce a medium-severity lead.
A success while at least five failures remain produces a high-severity lead.
Success resets the group's failure window. Events are sorted by timestamp,
then input order for ties. Evidence lines refer to the original file.

Subsequent failures do not produce an alert per event. A new burst is eligible
when the pending count falls below five or a success resets it. Log boundaries
reset all correlation state.

Limits: 2 MiB per file, 10,000 events, 16 KiB per line, 256 characters per required
string field, 100 displayed alerts, and 20 displayed evidence lines per alert.
Actual counts and truncation flags remain available. The bounded reader rejects
nonregular files, symlink swaps while opening, oversize files, and observed
mutation. Parsing and SHA-256 use the same bytes.

Reports omit raw usernames, hosts, source addresses, and free-text messages.
Identity digests are unsalted hashes: they permit correlation and guessing and
are not anonymization. Relative filenames, timestamps, and line references may
also be sensitive. Optional summary checkpoints stay under
`.jarvis-v2/auth-triage` with owner-only permissions.

These rules do not prove intrusion, identify an attacker, inspect MFA, detect
cross-user spraying, correlate distributed sources, analyze malware, or contain
a host. Zero alerts means these two patterns were absent from the accepted
input, not that the system is secure.

## Reproduce the detector benchmark

```bash
./venv/bin/python scripts/benchmark_v2_auth_triage.py
```

Ten synthetic cases cover bursts, later success, below-threshold and
outside-window activity, separated users/hosts/sources, embedded instructions,
duplicate IDs, and unordered input. Independent expectations check exact rule
IDs and evidence lines, event counts, and artifact digests. A mismatch exits 2.
Output states `kind: component_regression_only`, `model_used: false`, and
`capability_rating: null`. This does not award a broad 8/10 cyber or model rating.

## Live model finding — 2026-09-06

The first Qwen3-8B-4bit free-form trial returned correct counts but suggested
password spraying although the fixture covers one user. We changed the summary
contract to structured JSON with independent count/citation checks. The rerun
completed with six events, two alerts and exact citations, `facts_verified: true`:
local run `527ce239a8cd4707ae93c8fc3716b57b`, 983 prompt tokens, 270 completion
tokens. It reported leads rather than confirmed intrusion. This proves factual
extraction on one fixture; investigative judgment needs varied held-out cases.
