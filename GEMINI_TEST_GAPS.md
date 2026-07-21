# GEMINI-2 — Test Coverage Audit

Date: 2026-07-21. Method: AST extraction of every public top-level function and
public method of public classes in `harness/*.py`, `router.py`, `orchestrator.py`,
`operative.py`, `execution_engine.py`, `task_planner.py`, `model_router.py`,
cross-referenced by identifier against the full text of `tests/*.py` (185 test
files). A name referenced anywhere in tests counts as covered, so this is a
conservative lower bound on gaps — anything listed below has literally zero
mentions in the suite.

## Uncovered public functions (all findings)

| Module | Uncovered | Risk | Why |
|---|---|---|---|
| `harness/task_contract.py` | `normalize_task_id`, `is_sha256_digest`, `TaskBudget.from_value` | **HIGH** | Contract integrity primitives. `normalize_task_id` is the input gate for task IDs entering queue/lease state (rejects traversal-style IDs); `is_sha256_digest` guards contract/spec digest binding; `TaskBudget.from_value` sets attempt/wall-time/tool-call ceilings — a silent default regression removes budget enforcement. |
| `harness/agent_coordinator.py` | `default_state_path`, `clear_cooldown`, `build_parser` | **HIGH** | Cross-agent coordination. `clear_cooldown` is the only recovery path out of agent cooldown — if it fails to strip `cooldown_until`, an agent lane stays dead. `build_parser` defines the CLI contract every scheduled worker (this one included) depends on. |
| `harness/audit.py` | `list_snapshots`, `restore_snapshot`, `start_session`, `end_session` | **HIGH** | `restore_snapshot` is the memory disaster-recovery path — the worst possible place to discover a bug is during a restore. `list_snapshots` feeds the restore UX (ordering, missing-meta tolerance). |
| `harness/circuit_breaker.py` | `write_status_snapshot` | **MED** | Dashboard health mirroring. Documented to preserve unrelated keys in `ORCHESTRATOR_STATUS.json`; a regression clobbers queue-depth state written by `request_queue`. |
| `harness/request_queue.py` | `should_queue`, `start_drain_thread` | **MED** | `should_queue` decides park-vs-fail on total provider exhaustion; wrong answer at max depth means unbounded queue growth or hard failures while capacity exists. |
| `harness/watcher.py` | `unwatch`, `unwatch_all`, `list_watches` | **MED** | Watch lifecycle teardown; leaks observers if broken. Not testable in this sandbox (`watchdog` not installed) — see follow-ups. |
| `harness/self_eval_log.py` | `score_async` | **LOW** | Thin fire-and-forget thread wrapper around covered `score`. |
| `harness/adaptive_router.py` | `notify_route_used` | **LOW** | Currently a `_maybe_refresh()` pass-through; trivial body. |
| `orchestrator.py` | `ToolDecision.high_confidence` | **MED** | Routing confidence gate, but module import requires `anthropic` — not importable hermetically in the scheduled sandbox. |
| `operative.py` | `run_task_async` | **MED** | Same import constraint (`anthropic`). |
| `model_router.py` | `mobile_web_override`, `refresh_local_cache` | **MED** | Same import constraint (`openai`). |

Modules with zero uncovered public functions (by this method): all remaining
`harness/*.py`, `router.py`, `execution_engine.py`, `task_planner.py`.

## The 10 tests added — `tests/test_gemini_coverage.py`

Each test maps to exactly one gap above. All hermetic: no network, no Ollama,
filesystem isolated to `tmp_path`, module state patched via `monkeypatch`.

| # | Test | Gap closed |
|---|---|---|
| 1 | `test_normalize_task_id_accepts_valid_identifier` | `task_contract.normalize_task_id` happy path (strip + charset) |
| 2 | `test_normalize_task_id_rejects_invalid_values` | `normalize_task_id` rejection of empty/space/traversal/over-length IDs |
| 3 | `test_is_sha256_digest_accepts_only_canonical_lowercase_hex` | `is_sha256_digest` canonical-form enforcement (rejects uppercase, wrong length) |
| 4 | `test_task_budget_from_value_defaults_and_overrides` | `TaskBudget.from_value` default ceilings (3/1800/40) and mapping overrides |
| 5 | `test_clear_cooldown_restores_agent_availability` | `agent_coordinator.clear_cooldown` — cooldown keys removed, status persisted as available |
| 6 | `test_build_parser_parses_claim_command` | `agent_coordinator.build_parser` claim subcommand contract |
| 7 | `test_list_snapshots_orders_newest_first_and_tolerates_missing_meta` | `audit.list_snapshots` ordering + graceful missing-metadata degradation |
| 8 | `test_restore_snapshot_round_trip_and_missing_dir` | `audit.restore_snapshot` full restore of `memory.json` + nested `memory/`, and missing-dir error path |
| 9 | `test_should_queue_respects_enable_flag_and_max_depth` | `request_queue.should_queue` disable flag and max-depth refusal |
| 10 | `test_write_status_snapshot_records_health_and_preserves_keys` | `circuit_breaker.write_status_snapshot` health mirroring without clobbering unrelated status keys |

## Remaining gaps (follow-up candidates, priority order)

1. `audit.start_session` / `end_session` — mutate module globals and register
   `atexit` hooks; need a subprocess-based test to stay hermetic.
2. `watcher.unwatch` / `unwatch_all` / `list_watches` — needs `watchdog`
   installed in the test environment (present on the dev Mac; absent in the
   scheduled sandbox).
3. `orchestrator.ToolDecision.high_confidence`, `model_router.mobile_web_override`,
   `operative.run_task_async` — require stubbing `anthropic`/`openai` at import
   (conftest CI-stub pattern could be extended).
4. `request_queue.start_drain_thread` — idempotency test needs careful thread
   teardown; low value relative to flake risk in CI.
