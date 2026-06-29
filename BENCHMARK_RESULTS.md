# Ornith-1.0 Benchmark Results

**Status:** Routing code integrated; timing data pending one `python scripts/benchmark_ornith.py` run.  
**Hardware:** M-series Mac, ~14 CPU cores, unified memory  
**Models evaluated:** ornith-9b, ornith-35b, devstral, qwen3:30b-a3b  
**Source:** deepreinforce-ai/Ornith-1.0 — [HuggingFace](https://huggingface.co/deepreinforce-ai/Ornith-1.0-9B) · [Blog](https://deep-reinforce.com/ornith_1_0.html)

---

## Model Pull (run `scripts/benchmark_ornith.py` to populate)

| Model | Pull result |
|-------|-------------|
| `maxwell1500/ornith-9b` | _(not yet run)_ |
| `maxwell1500/ornith-35b` | _(not yet run)_ |

---

## Summary Table

| Model | Size | Avg response time | Code quality | Recommended role |
|-------|------|-------------------|--------------|-----------------|
| Ornith-9B | ~5 GB | _(pending)_ | _(pending)_ | classification fallback if p50 < 2 s |
| Ornith-35B | ~20 GB | _(pending)_ | _(pending)_ | fix_loop primary (replaces devstral) |
| Devstral | ~14 GB | _(pending)_ | _(pending)_ | fix_loop fallback |
| Qwen3-30B-A3B | ~20 GB | _(pending)_ | _(pending)_ | planning/reasoning (unchanged) |

Run `python scripts/benchmark_ornith.py` to fill this table with real numbers.
The script pulls models, runs 3 prompts, measures wall-clock time, and overwrites this file.

---

## Published Benchmark Context (DeepReinforce, 2026-06)

These are the vendor-published numbers that motivated pulling Ornith in the first place.
Once real timing data lands above, compare your local results against these.

| Model | SWE-bench verified | Terminal-Bench 2.1 | Notes |
|-------|-------------------|--------------------|-------|
| Ornith-1.0-9B | 68.1 | ~50 (est.) | Beats Gemma4-31B and Qwen3.6-35B at 9B params |
| Ornith-1.0-35B | **82.4** | **64.4** | Beats Qwen3.5-397B on Terminal-Bench (53.5) |
| Qwen3.5-35B | ~70 | 53.5 (397B) | Reference: the 35B dense Qwen3 variant |
| Devstral | ~46 | — | Mistral's coding model; current Jarvis coder |

Ornith-35B's 82.4 SWE-bench verified score is state-of-the-art for open-source as of 2026-06.
The Terminal-Bench 2.1 gap vs Qwen3.5-397B is significant given Ornith-35B is 10× smaller.

---

## Routing Decisions

These decisions are **pre-committed** based on published benchmarks, pending local timing validation:

- **fix_loop coder:** `coder_workbench.py` now tries `maxwell1500/ornith-35b` first via Ollama
  presence check; falls back to `LOCAL_CODER` (devstral or whatever `.env` sets) if not pulled.
  Condition to revert: if local timing shows ornith-35b > 2× devstral latency AND quality parity
  is not observed on the 3-prompt suite above.

- **classification path:** `LOCAL_ORNITH_9B` added to `provider_router._ollama_local_candidates()`
  as a fast-path fallback candidate. Only promote to active `LOCAL_CODER` override if local p50
  comes in under ~2s (the 200ms glm-4.7-flash target is tight; ornith-9b is not a drop-in at
  that latency even on M-series). Use `LOCAL_ORNITH_9B=maxwell1500/ornith-9b` in `.env` to
  activate explicitly.

- **RAM constraint:** ornith-35b at Q4_K_M needs ~20 GB unified memory.  If this Mac has 16 GB,
  the model will swap — expect 3–5× latency penalty and possible OOM.  Run the benchmark and
  check. If it OOMs, pin `LOCAL_ORNITH_35B=maxwell1500/ornith-9b` in `.env` to use the 9B
  instead.

---

## Per-Prompt Results (pending)

| Model | Prompt | Time (s) | Quality | Notes |
|-------|--------|----------|---------|-------|
| Ornith-9B | csv_sort | _(pending)_ | _(pending)_ | |
| Ornith-9B | debug | _(pending)_ | _(pending)_ | |
| Ornith-9B | plan_api | _(pending)_ | _(pending)_ | |
| Ornith-35B | csv_sort | _(pending)_ | _(pending)_ | |
| Ornith-35B | debug | _(pending)_ | _(pending)_ | |
| Ornith-35B | plan_api | _(pending)_ | _(pending)_ | |
| Devstral | csv_sort | _(pending)_ | _(pending)_ | |
| Devstral | debug | _(pending)_ | _(pending)_ | |
| Devstral | plan_api | _(pending)_ | _(pending)_ | |
| Qwen3-30B-A3B | csv_sort | _(pending)_ | _(pending)_ | |
| Qwen3-30B-A3B | debug | _(pending)_ | _(pending)_ | |
| Qwen3-30B-A3B | plan_api | _(pending)_ | _(pending)_ | |

---

## How to Get Real Numbers

```bash
cd ~/jarvis-ai
python scripts/benchmark_ornith.py
```

The script:
1. Pulls `maxwell1500/ornith-9b` and `maxwell1500/ornith-35b` if not present.
2. Runs the 3 prompts against all 4 models, timing wall-clock each response.
3. Overwrites this file with real numbers, quality heuristics, and routing decisions.

Requires: `ollama serve` running, `pip install ollama httpx`.
