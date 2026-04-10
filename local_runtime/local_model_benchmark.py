from __future__ import annotations

import time

from brains.brain_ollama import ask_local, list_local_models


DEFAULT_PROMPTS = [
    "Summarize optimistic locking versus pessimistic locking in two concise paragraphs.",
    "Explain how to debug a memory leak in a Python service with a concrete step-by-step plan.",
    "Write a short incident update message for a malware alert triage case.",
]


APPLE_SILICON_RECOMMENDATIONS = [
    {"model": "qwen2.5-coder:7b", "fit": "coding-heavy", "notes": "Strong code quality per watt, good on consumer Apple Silicon."},
    {"model": "llama3.1:8b", "fit": "general assistant", "notes": "Balanced quality and speed for local daily use."},
    {"model": "mistral:7b", "fit": "fast low-latency", "notes": "Good responsiveness for interactive assistant loops."},
    {"model": "qwen2.5:14b", "fit": "higher reasoning", "notes": "Better reasoning if memory budget allows a larger quantized model."},
]


def run_benchmark(prompts: list[str] | None = None, repeats: int = 1) -> dict:
    models = list_local_models()
    prompts = prompts or DEFAULT_PROMPTS
    repeats = max(1, int(repeats))
    if not models:
        return {"ok": False, "error": "No local models found. Pull at least one model with Ollama first."}

    rows: list[dict] = []
    for model in models:
        latencies_ms: list[int] = []
        output_chars = 0
        failures = 0
        for _ in range(repeats):
            for prompt in prompts:
                started = time.time()
                text = ask_local(prompt, model=model)
                elapsed_ms = int((time.time() - started) * 1000)
                latencies_ms.append(elapsed_ms)
                output_chars += len((text or "").strip())
                if (text or "").lower().startswith("local model error:"):
                    failures += 1
        avg_ms = int(sum(latencies_ms) / len(latencies_ms)) if latencies_ms else 0
        p95_index = max(0, int(len(latencies_ms) * 0.95) - 1)
        p95_ms = sorted(latencies_ms)[p95_index] if latencies_ms else 0
        rows.append(
            {
                "model": model,
                "runs": len(latencies_ms),
                "avg_latency_ms": avg_ms,
                "p95_latency_ms": p95_ms,
                "output_chars": output_chars,
                "failures": failures,
            }
        )

    rows.sort(key=lambda row: (row["failures"], row["avg_latency_ms"], -row["output_chars"]))
    winner = rows[0] if rows else {}
    return {"ok": True, "rows": rows, "winner": winner, "prompt_count": len(prompts), "repeats": repeats}


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
