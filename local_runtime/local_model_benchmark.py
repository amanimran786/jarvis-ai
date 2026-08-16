from __future__ import annotations

import math
import time

from brains.brain_ollama import get_client, list_local_models
from config import LOCAL_CLASSIFIER, LOCAL_CODER, LOCAL_DEFAULT, LOCAL_REASONING
from local_model_identity import find_exact_ollama_model


DEFAULT_PROMPTS = [
    "Summarize optimistic locking versus pessimistic locking in two concise paragraphs.",
    "Explain how to debug a memory leak in a Python service with a concrete step-by-step plan.",
    "Write a short incident update message for a malware alert triage case.",
]


APPLE_SILICON_RECOMMENDATIONS = [
    {"model": "qwen3.5:4b", "fit": "structured classification", "notes": "3.4GB and measured at 6/6 on the Jarvis tool-schema probe."},
    {"model": "qwen3:8b", "fit": "fast general chat", "notes": "5.2GB with the best measured ordinary-response latency on this M4 Pro."},
    {"model": "qwen3.6:35b", "fit": "coding-heavy", "notes": "23GB MoE and faster than the prior Devstral coding worker on the bounded Jarvis probe."},
    {"model": "qwen3:30b-a3b", "fit": "deep reasoning", "notes": "18GB MoE and faster than Qwen 3.6 on the bounded reasoning-worker probe."},
]


def _benchmark_models(
    installed: list[str],
    requested: list[str] | None,
) -> list[str]:
    preferred = requested or list(
        dict.fromkeys((LOCAL_CLASSIFIER, LOCAL_DEFAULT, LOCAL_CODER, LOCAL_REASONING))
    )
    selected = []
    for model in preferred:
        exact = find_exact_ollama_model(model, installed)
        if exact and exact not in selected:
            selected.append(exact)
    return selected


def run_benchmark(
    prompts: list[str] | None = None,
    repeats: int = 1,
    *,
    models: list[str] | None = None,
    max_context: int = 4096,
    max_output: int = 128,
) -> dict:
    installed = list_local_models()
    selected_models = _benchmark_models(installed, models)
    prompts = prompts or DEFAULT_PROMPTS
    repeats = max(1, int(repeats))
    max_context = max(512, int(max_context))
    max_output = max(16, min(int(max_output), 512))
    if not selected_models:
        return {
            "ok": False,
            "error": "None of the configured local role models are installed.",
        }

    rows: list[dict] = []
    client = get_client()
    for model in selected_models:
        latencies_ms: list[int] = []
        load_latencies_ms: list[int] = []
        tokens_per_second: list[float] = []
        output_chars = 0
        failures = 0
        for _ in range(repeats):
            for prompt in prompts:
                started = time.monotonic()
                try:
                    response = client.chat(
                        model=model,
                        messages=[{"role": "user", "content": prompt}],
                        stream=False,
                        think=False,
                        options={
                            "num_ctx": max_context,
                            "num_predict": max_output,
                            "temperature": 0,
                        },
                        keep_alive="5m",
                    )
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    text = (response.message.content or "").strip()
                    load_latencies_ms.append(
                        int((getattr(response, "load_duration", 0) or 0) / 1_000_000)
                    )
                    eval_count = int(getattr(response, "eval_count", 0) or 0)
                    eval_duration = int(getattr(response, "eval_duration", 0) or 0)
                    if eval_count > 0 and eval_duration > 0:
                        tokens_per_second.append(
                            eval_count / (eval_duration / 1_000_000_000)
                        )
                    output_chars += len(text)
                    if not text:
                        failures += 1
                except Exception:
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    failures += 1
                latencies_ms.append(elapsed_ms)
        avg_ms = int(sum(latencies_ms) / len(latencies_ms)) if latencies_ms else 0
        p95_index = max(0, math.ceil(len(latencies_ms) * 0.95) - 1)
        p95_ms = sorted(latencies_ms)[p95_index] if latencies_ms else 0
        rows.append(
            {
                "model": model,
                "runs": len(latencies_ms),
                "avg_latency_ms": avg_ms,
                "p95_latency_ms": p95_ms,
                "avg_load_latency_ms": (
                    int(sum(load_latencies_ms) / len(load_latencies_ms))
                    if load_latencies_ms else 0
                ),
                "avg_tokens_per_second": (
                    round(sum(tokens_per_second) / len(tokens_per_second), 2)
                    if tokens_per_second else 0.0
                ),
                "output_chars": output_chars,
                "failures": failures,
            }
        )

    rows.sort(key=lambda row: (row["failures"], row["avg_latency_ms"], -row["output_chars"]))
    winner = rows[0] if rows else {}
    return {
        "ok": True,
        "rows": rows,
        "winner": winner,
        "prompt_count": len(prompts),
        "repeats": repeats,
        "max_context": max_context,
        "max_output": max_output,
    }


def result_text(result: dict) -> str:
    if not result.get("ok"):
        return result.get("error", "Local benchmark failed.")
    rows = result.get("rows", [])
    if not rows:
        return "No benchmark rows were produced."
    top = rows[0]
    summary = (
        f"Ran {len(rows)} local models across {result.get('prompt_count', 0)} prompts x {result.get('repeats', 1)} repeat(s). "
        f"Best model was {top['model']} with average latency {top['avg_latency_ms']} ms and p95 latency {top['p95_latency_ms']} ms "
        f"with {top['failures']} failure(s)."
    )
    details = " ".join(
        f"{row['model']}: avg {row['avg_latency_ms']} ms, p95 {row['p95_latency_ms']} ms, failures {row['failures']}."
        for row in rows[:4]
    )
    return f"{summary} {details}"


def recommendation_text() -> str:
    chunks = []
    for row in APPLE_SILICON_RECOMMENDATIONS:
        chunks.append(f"{row['model']} is best for {row['fit']} and {row['notes']}")
    return "For Apple Silicon local-first use, recommended starting points are: " + " ".join(chunks)
