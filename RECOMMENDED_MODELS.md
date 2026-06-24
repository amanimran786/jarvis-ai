# Recommended Ollama Models — M4 Pro 48 GB

Hardware profile: Apple M4 Pro · 14-core (10P + 4E) · 48 GB unified memory.

With 48 GB of unified RAM you can run 30–32B parameter models at Q4 quantization
without swapping. The lineup below covers every routing tier Jarvis uses.

---

## Currently installed (no action needed)

| Model | Size | Role |
|---|---|---|
| `glm-4.7-flash:latest` | 19 GB | Primary — general, coding, long-context reasoning (202K ctx) |
| `qwen3:8b` | 5.2 GB | Medium fallback — fast general reasoning (32K ctx) |
| `jarvis-local:latest` | 4.9 GB | Fine-tuned Jarvis personality layer |
| `nomic-embed-text:latest` | 274 MB | Semantic memory embeddings — do not remove |
| `llava:7b` | 4.7 GB | Local vision / screenshot analysis |

---

## Tier 1 — Strong reasoning & long context (~20 GB, highest priority)

### `qwen3:30b-a3b` — Best quality/speed on Apple Silicon
MoE architecture: 30B total params but only ~3B active per token. Runs fast, fits
comfortably in 48 GB alongside GLM 4.7 Flash. Native 131K context window.
Outperforms dense 14B models on most benchmarks.

```bash
ollama pull qwen3:30b-a3b
```

Jarvis wires this automatically as `LOCAL_QWEN3_STRONG` for deep reasoning tasks.

---

## Tier 2 — Fast coding (8–10 GB, second priority)

### `devstral:latest` — Mistral's open-source coding model
Purpose-built for code: multi-file edits, repo-level context, test generation.
32K context. Fast on M4.

```bash
ollama pull devstral
```

Jarvis uses this as `LOCAL_DEVSTRAL` — routed automatically for code tasks when available.

### `qwen2.5-coder:32b` — Heavy coding (optional, 20 GB)
If you want a dedicated 32B coder (slower than devstral on first token but higher
ceiling for large refactors):

```bash
ollama pull qwen2.5-coder:32b
```

---

## Tier 3 — Lightweight / fast fallback (2–5 GB)

### `phi4-mini:latest` — Microsoft Phi-4 Mini
Tiny but punches above its weight on instruction-following tasks. Good for quick
lookups, simple questions, and low-latency use cases. 4K context.

```bash
ollama pull phi4-mini
```

Jarvis uses this as `LOCAL_PHI4_MINI` — routed for fast/simple tasks.

---

## Full pull sequence (recommended order)

Run these in sequence — each one starts downloading independently once queued:

```bash
# Required for best routing quality — do this first
ollama pull qwen3:30b-a3b

# Coding specialist
ollama pull devstral

# Fast lightweight fallback
ollama pull phi4-mini
```

After pulling, Jarvis auto-detects new models via `ollama list` (30-second TTL cache).
No config changes needed — the routing picks up new models automatically.

---

## RAM headroom with full fleet installed

| Model | Size | Cumulative |
|---|---|---|
| glm-4.7-flash | 19 GB | 19 GB |
| qwen3:30b-a3b | ~20 GB | 39 GB |
| qwen3:8b | 5.2 GB | 44 GB |
| devstral | ~3 GB | 47 GB |
| phi4-mini | ~2.5 GB | 49.5 GB |

Only the active model needs to be in RAM at once — Ollama swaps eviction-eligible
models out after 5 minutes of inactivity. With the Jarvis keepalive thread running,
the currently active model stays pinned. RAM total above assumes worst case (all warm).

At 48 GB, expect Ollama + macOS to be comfortable with GLM + one other model hot
at the same time. The keepalive thread will pin whichever model Jarvis last used.

---

## Context window reference

| Model | Native ctx | Jarvis num_ctx | Jarvis target |
|---|---|---|---|
| glm-4.7-flash | 202K | 128K (GLM_CTX) | 96K |
| qwen3:30b-a3b | 131K | 128K | 96K |
| qwen3:8b | 32K | 32K | 24K |
| devstral | 32K | 32K | 24K |
| phi4-mini | 4K | default | 12K (capped by model) |

Override any limit via env: `GLM_CTX=65536`, `QWEN3_LARGE_CTX=65536`, etc.
