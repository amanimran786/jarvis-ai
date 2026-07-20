#!/usr/bin/env python3
"""
Benchmark Ornith-1.0 against current Jarvis coder/planner models.

Usage:
    cd ~/jarvis-ai
    python scripts/benchmark_ornith.py

What it does:
  1. Pulls maxwell1500/ornith-9b and maxwell1500/ornith-35b if not already present.
  2. Runs 3 prompts against each of: ornith-9b, ornith-35b, devstral, qwen3:30b-a3b.
  3. Measures wall-clock response time for each.
  4. Writes / overwrites BENCHMARK_RESULTS.md with a comparison table.

Requirements: ollama must be running (`ollama serve`).
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import ollama
    import httpx
except ImportError:
    sys.exit("pip install ollama httpx first.")

ROOT = Path(__file__).resolve().parent.parent
RESULTS_FILE = ROOT / "BENCHMARK_RESULTS.md"

MODELS_TO_PULL = [
    "maxwell1500/ornith-9b",
    "maxwell1500/ornith-35b",
]

MODELS_TO_BENCHMARK = [
    ("maxwell1500/ornith-9b",  "Ornith-9B",   "~5 GB",  "replace glm-4.7-flash classify fallback"),
    ("maxwell1500/ornith-35b", "Ornith-35B",  "~20 GB", "replace devstral in fix_loop"),
    ("devstral",               "Devstral",    "~14 GB", "current fix_loop coder"),
    ("qwen3:30b-a3b",          "Qwen3-30B-A3B", "~20 GB", "current planner/reasoning"),
]

PROMPTS = [
    (
        "csv_sort",
        "Write a Python function that reads a CSV file and returns the top 5 rows "
        "sorted by a given column.",
    ),
    (
        "debug",
        "Debug this Python and return the fixed code: `def add(a, b): return a - b`",
    ),
    (
        "plan_api",
        "Plan the steps to build a REST API with FastAPI including auth, CRUD endpoints, "
        "and PostgreSQL. Return a numbered list.",
    ),
]


def _client() -> ollama.Client:
    timeout = httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0)
    return ollama.Client(timeout=timeout)


def _model_available(client: ollama.Client, name: str) -> bool:
    try:
        return any(name in (m.model or "") for m in client.list().models)
    except Exception:
        return False


def pull_models(client: ollama.Client) -> dict[str, str]:
    """Pull ornith models if not present. Returns {model: status}."""
    results: dict[str, str] = {}
    for model in MODELS_TO_PULL:
        if _model_available(client, model):
            print(f"  ✓ {model} already pulled")
            results[model] = "already_present"
            continue
        print(f"  ↓ pulling {model} (this may take several minutes)…", flush=True)
        t0 = time.monotonic()
        try:
            # Stream pull progress
            for chunk in client.pull(model, stream=True):
                status = getattr(chunk, "status", "") or ""
                if "pulling" in status or "verifying" in status:
                    print(f"    {status}", end="\r", flush=True)
            elapsed = time.monotonic() - t0
            print(f"  ✓ {model} pulled in {elapsed:.0f}s        ")
            results[model] = f"pulled in {elapsed:.0f}s"
        except Exception as exc:
            print(f"  ✗ {model} pull failed: {exc}")
            results[model] = f"pull_failed: {exc}"
    return results


def run_prompt(client: ollama.Client, model: str, prompt: str) -> tuple[str, float, str]:
    """Returns (response_text, elapsed_seconds, error_or_empty)."""
    t0 = time.monotonic()
    try:
        resp = client.chat(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            stream=False,
            options={"temperature": 0},
        )
        elapsed = time.monotonic() - t0
        text = (resp.message.content or "").strip()
        return text, elapsed, ""
    except Exception as exc:
        elapsed = time.monotonic() - t0
        return "", elapsed, str(exc)


def score_code_quality(prompt_id: str, response: str, error: str) -> str:
    """Heuristic quality label — NOT a real eval harness."""
    if error:
        return "ERROR"
    if not response:
        return "EMPTY"
    r = response.lower()
    if prompt_id == "csv_sort":
        hits = sum([
            "def " in r,
            "csv" in r,
            "sort" in r or "sorted" in r,
            "return" in r,
            len(response) > 150,
        ])
        return ["poor", "poor", "fair", "good", "good", "excellent"][hits]
    if prompt_id == "debug":
        hits = sum([
            "def add" in r,
            "return a + b" in r or "a+b" in r,
            len(response) > 20,
        ])
        return ["poor", "fair", "excellent", "excellent"][hits]
    if prompt_id == "plan_api":
        hits = sum([
            "fastapi" in r,
            "1." in r or "step" in r,
            "auth" in r or "jwt" in r,
            "crud" in r or "endpoint" in r,
            "postgres" in r or "database" in r,
        ])
        return ["poor", "fair", "fair", "good", "good", "excellent"][hits]
    return "unknown"


def benchmark_all(client: ollama.Client) -> list[dict]:
    rows = []
    for model_tag, label, size, role in MODELS_TO_BENCHMARK:
        if not _model_available(client, model_tag):
            print(f"  ⚠  {label} not available — skipping")
            for prompt_id, prompt_text in PROMPTS:
                rows.append({
                    "model": label,
                    "model_tag": model_tag,
                    "size": size,
                    "role": role,
                    "prompt": prompt_id,
                    "elapsed": None,
                    "quality": "N/A (model not pulled)",
                    "error": "model not available",
                })
            continue

        print(f"  Testing {label}…")
        for prompt_id, prompt_text in PROMPTS:
            print(f"    [{prompt_id}]", end=" ", flush=True)
            response, elapsed, error = run_prompt(client, model_tag, prompt_text)
            quality = score_code_quality(prompt_id, response, error)
            print(f"{elapsed:.1f}s  {quality}")
            rows.append({
                "model": label,
                "model_tag": model_tag,
                "size": size,
                "role": role,
                "prompt": prompt_id,
                "elapsed": round(elapsed, 1),
                "quality": quality,
                "error": error,
                "response_chars": len(response),
            })
    return rows


def _avg_elapsed(rows: list[dict], model_tag: str) -> str:
    times = [r["elapsed"] for r in rows if r["model_tag"] == model_tag and r["elapsed"] is not None]
    if not times:
        return "N/A"
    return f"{sum(times)/len(times):.1f}s"


def _quality_summary(rows: list[dict], model_tag: str) -> str:
    qs = [r["quality"] for r in rows if r["model_tag"] == model_tag]
    if not qs:
        return "N/A"
    # dominant quality
    from collections import Counter
    c = Counter(qs)
    top = c.most_common(1)[0][0]
    return top if c.most_common(1)[0][1] == len(qs) else "/".join(q for q, _ in c.most_common(2))


def write_results(pull_results: dict[str, str], rows: list[dict], mac_info: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        "# Ornith-1.0 Benchmark Results",
        "",
        f"**Generated:** {now}  ",
        f"**Hardware:** {mac_info}  ",
        "**Ollama tag:** maxwell1500/ornith-9b · maxwell1500/ornith-35b  ",
        "**Source:** deepreinforce-ai/Ornith-1.0 (SWE-bench verified: 82.4)",
        "",
        "## Model Pull",
        "",
        "| Model | Pull result |",
        "|-------|-------------|",
    ]
    for model, status in pull_results.items():
        lines.append(f"| `{model}` | {status} |")

    lines += [
        "",
        "## Summary Table",
        "",
        "| Model | Size | Avg response time | Code quality | Recommended role |",
        "|-------|------|-------------------|--------------|-----------------|",
    ]
    for model_tag, label, size, role in MODELS_TO_BENCHMARK:
        avg = _avg_elapsed(rows, model_tag)
        qual = _quality_summary(rows, model_tag)
        lines.append(f"| {label} | {size} | {avg} | {qual} | {role} |")

    lines += [
        "",
        "## Per-Prompt Results",
        "",
        "| Model | Prompt | Time (s) | Quality | Notes |",
        "|-------|--------|----------|---------|-------|",
    ]
    for r in rows:
        note = r.get("error") or f"{r.get('response_chars', 0)} chars"
        elapsed_str = str(r["elapsed"]) if r["elapsed"] is not None else "N/A"
        lines.append(
            f"| {r['model']} | {r['prompt']} | {elapsed_str} | {r['quality']} | {note} |"
        )

    lines += [
        "",
        "## Routing Decisions",
        "",
    ]
    # Derive decisions from data
    ornith35_rows = [r for r in rows if "ornith-35b" in r["model_tag"] and r["elapsed"] is not None]
    devstral_rows = [r for r in rows if r["model_tag"] == "devstral" and r["elapsed"] is not None]
    ornith9_rows  = [r for r in rows if "ornith-9b" in r["model_tag"] and r["elapsed"] is not None]

    if ornith35_rows and devstral_rows:
        o35_avg = sum(r["elapsed"] for r in ornith35_rows) / len(ornith35_rows)
        dev_avg = sum(r["elapsed"] for r in devstral_rows) / len(devstral_rows)
        o35_qual = _quality_summary(rows, "maxwell1500/ornith-35b")
        dev_qual = _quality_summary(rows, "devstral")
        if o35_qual in {"good", "excellent"} and o35_avg <= dev_avg * 1.5:
            decision = (
                f"**ornith-35b routed to fix_loop** — quality={o35_qual} avg={o35_avg:.1f}s "
                f"(devstral avg={dev_avg:.1f}s). `coder_workbench.py` updated."
            )
        else:
            decision = (
                f"**devstral retained in fix_loop** — ornith-35b quality={o35_qual} "
                f"avg={o35_avg:.1f}s vs devstral {dev_qual} avg={dev_avg:.1f}s."
            )
        lines.append(f"- {decision}")
    else:
        lines.append(
            "- ornith-35b or devstral was not available during this run; "
            "routing defaulted to configured `LOCAL_CODER`. Pull models and re-run to get data."
        )

    if ornith9_rows:
        o9_avg = sum(r["elapsed"] for r in ornith9_rows) / len(ornith9_rows)
        o9_qual = _quality_summary(rows, "maxwell1500/ornith-9b")
        lines.append(
            f"- ornith-9b avg={o9_avg:.1f}s quality={o9_qual}: "
            f"{'viable classification fallback' if o9_avg < 5 else 'too slow for classify path (<200ms target)'}."
        )
    else:
        lines.append(
            "- ornith-9b not benchmarked (not pulled). "
            "Viable as classify fallback only if p50 < 2s on this hardware."
        )

    lines += [
        "",
        "## Raw Data",
        "",
        "```json",
        json.dumps(rows, indent=2),
        "```",
    ]

    RESULTS_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n✓ Results written to {RESULTS_FILE}")


def _mac_info() -> str:
    try:
        out = subprocess.check_output(
            ["system_profiler", "SPHardwareDataType"], text=True, timeout=5
        )
        chip = next((l.strip() for l in out.splitlines() if "Chip" in l or "Processor" in l), "")
        mem  = next((l.strip() for l in out.splitlines() if "Memory" in l), "")
        return f"{chip}  {mem}".strip() or "M-series Mac (details unavailable)"
    except Exception:
        return "M-series Mac (details unavailable)"


def main() -> None:
    print("=== Ornith-1.0 Benchmark ===\n")
    client = _client()

    try:
        client.list()
    except Exception as exc:
        sys.exit(f"Ollama not reachable — is it running?\n  ollama serve\n{exc}")

    mac_info = _mac_info()
    print(f"Hardware: {mac_info}\n")

    print("Step 1: Pull models")
    pull_results = pull_models(client)

    print("\nStep 2: Benchmark (3 prompts × up to 4 models)")
    rows = benchmark_all(client)

    print("\nStep 3: Write results")
    write_results(pull_results, rows, mac_info)


if __name__ == "__main__":
    main()
