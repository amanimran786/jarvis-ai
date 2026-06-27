# GLM 5.2 Routing Readiness Evaluation

Date: 2026-06-27 UTC

## Decision

**No. Do not set GLM 5.2 as a default in `model_router.py`.**

GLM 5.2 is not available to Jarvis as a local model on this machine, the configured
Ollama Cloud account cannot run it without a subscription, and no response-quality
evidence was produced. Promoting it would violate Jarvis's local-first default and
turn the normal path into a paid, currently inaccessible cloud dependency.

Keep `glm-4.7-flash` as the general local default. GLM 5.2 should remain an
evaluation-only candidate for a separately configured external-local endpoint that
has sufficient memory and an immutable model digest.

## Readiness Evidence

Command:

```bash
python -m pytest tests/test_glm52_readiness.py -v
```

Result: **5 passed in 1.10s**.

The readiness contract confirms:

- `LOCAL_GLM52_MODEL` is `glm-5.2`, but the candidate has no local pull command.
- The M4 Pro's 48 GiB is a no-go for the model's official weight profile.
- The conservative FP8 serving floor is 800 GiB; this host reports 48 GiB.
- The official Ollama model is cloud-only and no official local Ollama quant is available.
- Cloud-tagged or inexact models are ineligible for the external-local evaluation lane.
- Auto-promotion is explicitly disabled and an external endpoint must pin a digest.

`ollama list` does not contain GLM 5.2. It contains `glm-4.7-flash:latest`,
`qwen3:8b`, `jarvis-local:latest`, `nomic-embed-text:latest`, and `llava:7b`.
The runtime readiness report therefore returns `external_hardware_required`,
`eligible_for_eval=false`, `local_weight_fit=false`, and
`external_endpoint_memory_verified=false`.

## Five-Query Evaluation

The five representative prompts were sent through Jarvis's existing
`ask_ollama_cloud_stream(..., model="glm-5.2")` integration. The cloud model catalog
advertised `glm-5.2`, but every inference request returned HTTP 403 because this model
requires a subscription.

| Category | Representative behavior requested | Result | Quality score |
|---|---|---|---|
| Code | Typed Python parser plus focused pytest tests | Blocked before inference: HTTP 403 | Not measurable |
| Planning | Five dependency-aware implementation steps with verification | Blocked before inference: HTTP 403 | Not measurable |
| QA | Accurate, bounded explanation of SQLite WAL concurrency | Blocked before inference: HTTP 403 | Not measurable |
| Voice command | One-sentence confirmation after a successful timer tool call | Blocked before inference: HTTP 403 | Not measurable |
| Tool routing | JSON selection for a calendar query | Blocked before inference: HTTP 403 | Not measurable |

This is an access failure, not evidence that the model's answers are low quality.
It is decisive evidence that GLM 5.2 cannot be Jarvis's default in the current runtime.

## Existing Eval Data

`evals.json` contains no GLM 5.2 interactions. It contains five scored
`glm-4.7-flash` interactions, all repetitions of the same open-task query and response.
Their linked self-eval mean is 0.742 response quality, 0.500 routing accuracy,
0.667 relevance, and 1.000 conciseness. This is fixture-like coverage, not a diverse
quality baseline, so it should not be treated as a meaningful GLM 4.7 versus 5.2
comparison.

## Promotion Gate

Reconsider GLM 5.2 only after all of the following are true:

1. A non-cloud external-local endpoint exposes the exact configured model.
2. The serving host's memory is verified independently of this controller Mac.
3. `LOCAL_GLM52_DIGEST` pins and validates the endpoint model identity.
4. The five representative queries complete and are scored against the current default.
5. Focused routing tests prove that code-specialist and fast-model routes do not regress.

Until then, no `model_router.py` change is warranted.
