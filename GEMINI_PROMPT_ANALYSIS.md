# Prompt Quality Analysis — Self-Eval Logs

**Source data:** `logs/self_eval.jsonl` (882 records, `harness/self_eval_log.py` schema — routing/relevance/conciseness axes) and `evals/response_scores.jsonl` (917 records, `self_eval.py` schema — specificity/voice/contamination axes), covering 2026-06-25 to 2026-07-18.

**Method:** loaded both logs, aggregated composite/axis scores overall and per query, counted flags, and cross-checked the scoring source (`harness/self_eval_log.py`, `self_eval.py`) to explain *why* specific scores land where they do rather than guessing from the numbers alone.

## Executive Summary

- **The dataset is a 21-prompt synthetic benchmark replayed on a loop, not organic traffic.** 882 records collapse to 21 unique queries (max 107 repeats of "deploy the current Jarvis build"). Conclusions below describe this fixed health-check suite, not live user behavior — see Recommendation 3.
- **`poor_relevance` is the dominant failure, hitting 36.7% of all records (324/882)** and is fully concentrated in 5 of the 21 prompts, each flagged at 100% (or 82/83) across every single replay. The scores are bit-identical run over run (e.g. relevance = 0.300 every time for "Research best practices for vector database indexing"), which rules out model-response variance — it's a deterministic scoring-rule trip, not intermittent quality drift.
- **Root cause, traced through `_score_response_relevance`:** any query over 5 words paired with a response under 5 words auto-scores 0.30 relevance. The 5 affected prompts ("Research best practices...", "Write a pytest fixture...", "propose a skill for stale vault maintenance", "fix the auth middleware unit test", "distill recent runtime lessons into the roadmap") are all task-delegation-style commands that appear to get a terse stub acknowledgment instead of a substantive reply — while structurally similar prompts like "list open tasks and tell me which ones are blocked" (11 words) get a full response and score fine (0.667 relevance, no flag).
- **`routing_accuracy` is essentially unmeasured: `route` is empty in 869/882 records (98.5%)**, which forces the scorer's neutral default (0.5) rather than a real judgment. The mirrored `routing_tag` field in `evals/response_scores.jsonl` is empty in 915/917 (99.8%), so `module_correct` and `memory_util` there are computed on only 2 samples each. This isn't a routing problem — it's an instrumentation gap; whatever calls `score()`/`score_response()` for these benchmark runs isn't passing the route.
- **Composite quality (0.608/1.0) is arithmetically dragged down by relevance, not by conciseness.** `response_quality = routing(0.25)×0.504 + relevance(0.40)×0.517 + conciseness(0.35)×0.787`. Relevance carries the heaviest weight (0.40) and is the lowest-scoring axis — it's the single highest-leverage fix.

## Detail: the 5 flagged prompts

| query | n | avg quality | relevance | conciseness | flag rate |
|---|---:|---:|---:|---:|---:|
| Research best practices for vector database indexing | 81 | 0.427 | 0.300 | 0.520 | 100% |
| Write a pytest fixture for the auth module | 83 | 0.435 | 0.306 | 0.531 | 99% |
| propose a skill for stale vault maintenance | 56 | 0.534 | 0.300 | 0.825 | 100% |
| fix the auth middleware unit test | 48 | 0.591 | 0.300 | 0.987 | 100% |
| distill recent runtime lessons into the roadmap | 56 | 0.591 | 0.300 | 0.987 | 100% |

Contrast with structurally similar but *unflagged* multi-word prompts:

| query | n | avg quality | relevance | conciseness |
|---|---:|---:|---:|---:|
| list open tasks and tell me which ones are blocked | 49 | 0.742 | 0.667 | 1.000 |
| deploy the current Jarvis build | 107 | 0.657 | 0.598 | 0.839 |
| refactor the auth middleware | 56 | 0.784 | 0.783 | 0.987 |

The pattern: prompts that read as "go do research / write code / propose something / distill something" get short stub responses; prompts that read as "tell me status" get full answers. Note that `self_eval.jsonl` doesn't log response text (only length-derived signals), so this is inferred from the scoring math, not confirmed by reading the actual replies — worth fixing per Recommendation 3.

## Top 3 Actionable Improvements

### 1. Give task-delegation prompts a substantive acknowledgment, not a bare stub

This single fix addresses 324/882 flagged records (36.7% of the dataset) and is the highest-leverage lever given relevance's 0.40 composite weight.

**Before** (inferred — response under 5 words, e.g.):
```
On it.
```
```
Sure, working on it.
```

**After** (echoes query terms, states the concrete next step, clears the <5-word trap):
```
Researching vector DB indexing best practices now — comparing HNSW vs IVF
tradeoffs for our embedding scale. Will report back with a recommendation.
```
```
Drafting a pytest fixture for the auth module: mocking the token issuer,
covering expired/invalid-token cases, following the conftest.py pattern
already in tests/.
```

This isn't padding for its own sake — `_score_response_relevance` rewards term overlap with the query (`vector`, `database`/`db`, `indexing`; `pytest`, `fixture`, `auth`, `module`) and the length-based penalty only fires under 5 words, so a single concrete sentence clears both.

### 2. Pass `route`/`routing_tag` through on every self-eval call

**Before** (implicit — call site omits routing context):
```python
harness.self_eval_log.score(query, response)          # route defaults to ""
self_eval.score_response(query, response, context={})  # no routing_tag / user_input
```

**After:**
```python
harness.self_eval_log.score(query, response, route=resolved_route)
self_eval.score_response(
    query, response,
    context={"routing_tag": resolved_route, "user_input": query},
    routing_tag=resolved_route,
)
```
Without this, `routing_accuracy` and `module_correct` are structurally incapable of surfacing a real routing regression — they're reporting the neutral default for >98% of traffic, which would silently mask an actual routing break in this benchmark.

### 3. Diversify the benchmark and log response text (or a truncated snippet)

21 prompts replayed on a fixed interval can't detect prompt-quality regressions outside that set, and root-causing failures currently requires reverse-engineering scorer math instead of reading what was actually said. Recommend: (a) rotate in a sample of real production queries alongside the fixed suite, and (b) add a `response_snippet` (first ~200 chars) field to the log record so future analysis — including this kind — can verify root cause directly instead of inferring it from word-count math.

## Metric to Track Going Forward

Primary: **`poor_relevance` flag rate** (currently 36.7%) — target under 10% after fix #1. This is the single number most tied to the composite score given relevance's 0.40 weight.

Secondary (instrumentation health, not response quality): **% of records with non-empty `route`/`routing_tag`** (currently 1.5% / 0.2%) — until this clears ~95%, `routing_accuracy` and `module_correct` trends should be treated as noise, not signal.

Watch for regression: per-query flag rate held constant even as new prompts are added — a previously-clean prompt tripping `poor_relevance` for the first time is a more actionable signal than the aggregate composite moving by a few hundredths.
