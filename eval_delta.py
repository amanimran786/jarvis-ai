"""Local-vs-cloud eval delta lane.

Picks cases from `capability_evals` and runs each prompt through both the
local lane (Ollama) and the cloud lane (OpenAI via provider_priority's
gate-bypassing helper). Returns side-by-side answers so a human can judge
quality drift before committing to a fine-tune.

This module is opt-in: it costs cloud calls per case, so it should only be
invoked from the `--eval-delta` CLI flag, never on a schedule.
"""
from __future__ import annotations

from typing import Any

import capability_evals


def run_delta(group: str | None = None, limit: int = 3, tier: str = "strong") -> list[dict[str, Any]]:
    from brains import brain_ollama
    from config import GPT_FULL, LOCAL_REASONING

    try:
        from provider_priority import _ask_openai
    except Exception as exc:  # pragma: no cover - keeps runner importable in stub envs
        raise RuntimeError(f"eval_delta requires provider_priority: {exc}") from exc

    cases = capability_evals.list_cases(group)[: max(0, int(limit))]
    cloud_model = GPT_FULL
    # Local reasoning model is the only local lane today; tier kept for future
    # routing parity with model_router (cheap/strong/deep) without behavior change.
    local_model = LOCAL_REASONING
    _ = tier  # reserved

    results: list[dict[str, Any]] = []
    for case in cases:
        prompt = case["prompt"]
        row: dict[str, Any] = {
            "id": case["id"],
            "group": case["group"],
            "prompt": prompt,
            "local_model": local_model,
            "cloud_model": cloud_model,
        }
        try:
            row["local"] = brain_ollama.ask_local(prompt, model=local_model, raise_on_error=True)
        except Exception as exc:
            row["local"] = ""
            row["local_error"] = str(exc)
        try:
            row["cloud"] = _ask_openai(prompt, model=cloud_model)
        except Exception as exc:
            row["cloud"] = ""
            row["cloud_error"] = str(exc)
        results.append(row)
    return results


def format_delta(rows: list[dict[str, Any]]) -> str:
    """Render results as a single text block for console output."""
    if not rows:
        return "No eval cases matched."
    chunks: list[str] = []
    for row in rows:
        header = f"--- {row['group']}/{row['id']}  local={row['local_model']}  cloud={row['cloud_model']}"
        chunks.append(header)
        chunks.append(f"PROMPT: {row['prompt']}")
        chunks.append("LOCAL:")
        chunks.append((row.get("local") or f"ERROR: {row.get('local_error', '')}").strip())
        chunks.append("CLOUD:")
        chunks.append((row.get("cloud") or f"ERROR: {row.get('cloud_error', '')}").strip())
        chunks.append("")
    return "\n".join(chunks)
