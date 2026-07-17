# Local Specialist Routing Verification

Date: 2026-07-16 PDT  
Task: `jarvis-local-llm-routing-verify`  
Source revision: `3362e343207a21bee112f8651e2b160c40c3eb42`

## Verdict

**NOT READY**

Devstral routing and the real code workbench are production-functional. Qwen
`qwen3:30b-a3b` is installed, selected correctly, and stable under concurrent direct
inference, but the production planner fails because it reads only
`response.message.content` while Qwen returns its planning tokens in
`response.message.thinking` by default.

The specialist model-selection policy is correct after updating the ignored local
runtime settings. The Qwen planner integration must be fixed and re-run before this
capability can be called production-ready.

## Environment

- Ollama: reachable at `127.0.0.1:11434`.
- Required models installed during this verification:
  - `devstral:latest`, 14 GB
  - `qwen3:30b-a3b`, 18 GB
- Other relevant installed models: `glm-4.7-flash`, `qwen3:8b`,
  `jarvis-local`, and `maxwell1500/ornith-9b:Q4_K_M`.
- Runtime role settings after prerequisite repair:
  - coder: `devstral`
  - reasoning/planner: `qwen3:30b-a3b`
- Runtime mode: `open-source`
- No tracked model default was changed in `config.py`.

The ignored `.env` had continued to force both roles to `glm-4.7-flash` from the
period when the specialist models were absent. Those two non-secret local runtime
overrides were changed to the installed specialist models. Before that correction,
`local_capabilities()` truthfully reported GLM for both roles even though the
query-aware selector chose the specialists.

## Routing selection

The production query-aware selector `model_router._best_local()` was evaluated with
10 representative prompts per class after `refresh_local_cache()`.

| Query class | Expected model | Observed | Correct |
|---|---|---|---:|
| Code, test, debug, patch | `devstral` | 10/10 `devstral` | 100% |
| Deep planning, research, architecture | `qwen3:30b-a3b` | 10/10 `qwen3:30b-a3b` | 100% |

The direct production selectors also resolved correctly after the runtime override
repair:

- `get_best_available(LOCAL_CODER)` -> `devstral`
- `get_best_available(LOCAL_REASONING)` -> `qwen3:30b-a3b`
- `local_capabilities()["selected_coder"]` -> `devstral`
- `local_capabilities()["selected_reasoning"]` -> `qwen3:30b-a3b`

Relevant implementation:

- Code specialist order: `model_router.py:438-468`
- Deep-planning order: `model_router.py:470-474`
- Direct exact-preference selection: `brains/brain_ollama.py:469-485`
- Planner role selection: `task_planner.py:120-150`
- Workbench role selection: `coder_workbench.py:414-452`

## Load results

Each specialist received 10 real Ollama chat requests through a two-worker thread
pool. Prompts were intentionally short and responses were capped at 64 tokens so the
measurement tests routing and server stability rather than long-form generation.
Both groups included the first requests after switching the resident model.

### Devstral

| Metric | Result |
|---|---:|
| Requests | 10 |
| Concurrency | 2 |
| Successful non-empty responses | 10/10 |
| Total wall time | 23.679 s |
| Mean latency | 4.607 s |
| Median latency | 2.139 s |
| P95 latency | 14.300 s |
| Maximum latency | 15.271 s |
| Warm-request range | 1.513-2.686 s |

The first two requests absorbed model loading at 14.300 and 15.271 seconds. All
subsequent requests completed in under 2.7 seconds.

### Qwen3 30B A3B

This probe set `think=False`, which is the behavior the planner requires when it
expects JSON in `message.content`.

| Metric | Result |
|---|---:|
| Requests | 10 |
| Concurrency | 2 |
| Successful non-empty responses | 10/10 |
| Total wall time | 17.060 s |
| Mean latency | 3.309 s |
| Median latency | 1.876 s |
| P95 latency | 8.577 s |
| Maximum latency | 9.515 s |
| Warm-request range | 1.867-1.884 s |

The first two requests absorbed the model switch at 8.577 and 9.515 seconds. All
subsequent requests completed in under 1.9 seconds.

## Production-path checks

### Devstral code workbench: PASS

The real `coder_workbench.fix_loop()` ran in an isolated temporary workspace with
this task:

> Create `add.py` with an `add(a, b)` function and `test_add.py` with two pytest
> tests.

Observed result:

- Selected model: `devstral`
- End-to-end latency: 15.225 seconds
- Iterations: 1
- Files generated: `add.py`, `test_add.py`
- Generated test command: `python -m pytest test_add.py -q`
- Test result: 2 passed, 0 failed

No file from this probe was written into the repository.

### Qwen local planner: FAIL

The real `task_planner._plan_task_local()` was run against three representative
planning tasks. Each planner call allows up to three parse attempts.

| Probe | Attempts | Result | Latency |
|---|---:|---|---:|
| Async-framework research plan | 3 | failed, empty content | 20.915 s |
| Background-task root-cause plan | 3 | failed, empty content | 0.273 s |
| JSON-to-SQLite migration plan | 3 | failed, empty content | 0.264 s |

Result: 0/3 plans and 0/9 parse attempts succeeded.

## Root cause

`task_planner._plan_task_local()` calls `client.chat()` without a `think` argument
and then reads only `response.message.content` (`task_planner.py:147-165`). Qwen's
default response placed generated tokens in `message.thinking`, leaving `content`
empty. The parser then raised `No valid JSON plan in response` on every attempt.

A controlled call to the same model with `think=False` returned non-empty content.
The 10-request Qwen load probe using that setting succeeded 10/10.

The planner also supplies only `temperature=0`. Ollama therefore loaded Qwen at its
native 262,144-token context, reported as roughly 50 GB resident by `ollama ps`.
Jarvis already defines bounded Qwen context options in
`brains/brain_ollama.py:435-457`, but the planner's direct client path bypasses them.

## Required corrective work

1. In `task_planner._plan_task_local()`, call Qwen with `think=False` so JSON is
   emitted in `message.content`.
2. Reuse the model-specific bounded options from
   `brains.brain_ollama._ollama_options_for_model()` and set a planner-appropriate
   response cap. Do not load a 262K context for a small planning prompt.
3. Add a regression test whose response contains `thinking` and empty `content`, plus
   a live opt-in test that confirms a real Qwen plan parses.
4. Re-run the three production planner probes and the 10-request concurrency test.
5. Keep the Devstral workbench result as the baseline; no routing change is needed
   for code tasks.

## Automated test

Contract entry point:

```text
python -m pytest tests/test_agent_local_routing.py -q
```

Result: 5 passed, 0 failed. One SciPy/NumPy compatibility warning was emitted by an
optional sklearn import. These tests validate route policy with mocks; they did not
catch the live Qwen response-field mismatch.

The first full-suite gate under the old `.env` mode (`auto`) failed at
`RouterTests.test_fastapi_502_routes_to_specialized_agents`. The debugger's cold
Qwen load exceeded 45 seconds and the reviewer's cold GLM load exceeded 15 seconds;
the subsequent paid-cloud fallback returned text without the required diagnostic
terms. This confirms that switching between two heavyweight resident models is not
stable inside the current specialist deadlines.

After changing the ignored runtime mode to `open-source`, that focused regression
passed in 62.20 seconds. The same local timeouts still occur, but open-source mode
uses the deterministic local fallback instead of transmitting the prompt to a paid
provider. This is fail-closed behavior, not acceptable interactive latency; model
residency and role assignment still require follow-up tuning.

The exact full-suite command under `open-source` then failed at
`TaskPlannerSanitizerTests.test_plan_task_downgrades_unknown_tools_to_chat`. That
test mocks a local-planner failure and unconditionally expects the cloud planner's
two-step response, while production correctly returns a one-step local degraded plan
in `open-source` mode (`task_planner.py:247-264`). The test is coupled to the
operator's `.env` mode and must explicitly patch the intended mode. The source
regression gate was therefore also run under its historical `auto`/GLM baseline;
that baseline does not replace the live specialist measurements above. Baseline
result: 3,457 passed, 3 skipped, 34 subtests passed, 0 failed in 867.72 seconds.

## Stability decision

- Model availability: PASS
- Code-query selection: PASS
- Planning-query selection: PASS
- Devstral concurrent inference: PASS
- Devstral end-to-end code loop: PASS
- Qwen concurrent inference with `think=False`: PASS
- Qwen production planner: FAIL
- Local-only timeout behavior: PASS, but too slow (62.20 s focused regression)
- Full-suite mode isolation: FAIL (test assumes cloud fallback in open-source mode)
- Overall specialist routing capability: **NOT READY**
