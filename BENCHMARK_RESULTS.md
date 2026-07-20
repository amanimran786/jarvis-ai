# Model Speed Benchmark Results

**Run date:** 2026-06-29  
**Machine:** macOS (Apple Silicon unified memory)  
**Methodology:** Each prompt sent via Ollama streaming API or Ollama Cloud REST API. TTFT = time to first visible output token. Throughput = total eval tokens / total wall time. Models evicted from VRAM between runs (`keep_alive: 0`) to ensure clean baselines. `glm-4.7-flash` and `qwen3:8b` are thinking models — TTFT is post-think latency.

---

## Results

### TTFT — Time to First Visible Token (seconds)

| Model | simple_chat | code_gen | reasoning | classification | summarization |
|---|---|---|---|---|---|
| glm-4.7-flash (29.9B, GPU) | 7.89 | 12.85 | 8.72 | **3.92** | 6.06 |
| qwen3:8b (GPU→CPU†) | 20.18 | —‡ | —‡ | 4.12 | —‡ |
| jarvis-local (CPU) | 7.05 | 3.41 | 18.73 | 27.16 | 22.78 |
| llava:7b (GPU) | 2.66 | **0.11** | **0.10** | **0.10** | **0.11** |
| ollama_cloud/gemma4:31b | **0.34** | **0.35** | **0.31** | 0.33 | **0.42** |

† qwen3:8b ran partially CPU-bound — glm had not fully released VRAM when benchmarked.  
‡ ttft=None: model used all 400 allocated tokens on internal thinking without emitting visible output; truncated.

### Throughput (tokens/sec, where measurable)

| Model | simple_chat | code_gen | reasoning | classification | summarization |
|---|---|---|---|---|---|
| glm-4.7-flash | 56.7 | 54.9 | 54.0 | 53.3 | 53.1 |
| qwen3:8b | 10.6 | 8.8 | 12.8 | 35.2 | 25.5 |
| jarvis-local | 3.4 | 17.3 | 1.8 | 0.1 | 1.3 |
| llava:7b | 1.5* | 54.2 | 50.3 | 29.0 | 53.8 |
| ollama_cloud/gemma4:31b | ~5 | ~23 | ~28 | ~5 | ~12 |

*llava produced only 4 visible tokens for simple_chat (minimal greeting), making tok/s misleadingly low.

---

## Analysis

### glm-4.7-flash (LOCAL_CODER_MODEL / LOCAL_REASONING_MODEL)
- **Consistent GPU throughput ~55 tok/s** regardless of task.
- Thinking model: TTFT is 4–13s post-think. Classification is fastest (3.9s) because answers are short — thinking stops sooner.
- **Best model for quality-sensitive tasks** (coding, reasoning) where the user can wait 8–13s.
- Not suitable for latency-critical paths (voice wake-word reply, quick status checks).

### llava:7b
- **Fastest local TTFT for structured tasks** — 0.10–0.11s for code/reasoning/classification/summarization.
- Underperforms on open-ended chat (simple_chat produced only 4 tokens — weak instruction following for freeform prompts).
- **Best local option for classification, summarization, and code scaffolding** when already loaded in VRAM.
- Multimodal model; text-only performance is strong but chat alignment is weaker than purpose-built text models.

### ollama_cloud/gemma4:31b
- **Fastest TTFT overall** — 0.31–0.42s across all tasks, no cold-load penalty.
- Network-dependent; session/weekly free-tier limits apply (see `harness/budget.py`).
- **Best for latency-sensitive paths** (voice replies, quick tool classifications) when cloud budget is available.
- Falls back to local when `OLLAMA_CLOUD_ENABLED=false` or budget exhausted.

### qwen3:8b
- Benchmark ran under memory pressure (glm still partially occupying VRAM). Results understate true GPU speed.
- `think=false` passed correctly in options; some tasks still hit 400-token cap — model likely still reasons internally before emitting.
- Re-benchmark with clean VRAM to get accurate numbers. Expect ~30–50 tok/s when GPU-resident.

### jarvis-local
- Consistently slow: 1–17 tok/s across tasks, 7–27s TTFT.
- Low output token counts for reasoning/classification/summarization (3–34 tokens) suggest poor alignment on non-coding tasks.
- **Use only for quick code completions** where it's already warm in VRAM; prefer glm-4.7-flash or llava:7b otherwise.

---

## Routing Recommendations

| Task | Primary | Fallback | Avoid |
|---|---|---|---|
| Simple chat / voice reply | `ollama_cloud` (0.34s) | `llava:7b` | `jarvis-local` |
| Code generation | `glm-4.7-flash` (quality) | `llava:7b` (speed) | `qwen3:8b` (slow) |
| Reasoning / planning | `glm-4.7-flash` (thinking model) | `ollama_cloud` | `jarvis-local` |
| Classification / routing | `llava:7b` (0.10s) | `ollama_cloud` | `jarvis-local` |
| Summarization | `llava:7b` (0.11s) | `ollama_cloud` | `jarvis-local` |

---

## Raw Numbers

```
glm-4.7-flash (clean VRAM, 800-tok cap):
  simple_chat:    ttft=7.89s  total=7.92s   tok=449  tps=56.7
  code_gen:       ttft=12.85s total=13.81s  tok=758  tps=54.9
  reasoning:      ttft=8.72s  total=10.70s  tok=578  tps=54.0
  classification: ttft=3.92s  total=3.96s   tok=211  tps=53.3
  summarization:  ttft=6.06s  total=6.42s   tok=341  tps=53.1

qwen3:8b (think=false, partial VRAM pressure, 400-tok cap):
  simple_chat:    ttft=20.18s total=20.40s  tok=216  tps=10.6
  code_gen:       ttft=None   total=45.64s  tok=400  tps=8.8   [capped]
  reasoning:      ttft=None   total=31.22s  tok=400  tps=12.8  [capped]
  classification: ttft=4.12s  total=4.18s   tok=147  tps=35.2
  summarization:  ttft=None   total=15.66s  tok=400  tps=25.5  [capped]

jarvis-local (clean VRAM, 300-tok cap):
  simple_chat:    ttft=7.05s  total=7.61s   tok=26   tps=3.4
  code_gen:       ttft=3.41s  total=5.49s   tok=95   tps=17.3
  reasoning:      ttft=18.73s total=19.42s  tok=34   tps=1.8
  classification: ttft=27.16s total=27.21s  tok=3    tps=0.1
  summarization:  ttft=22.78s total=23.42s  tok=31   tps=1.3

llava:7b (clean VRAM, 300-tok cap):
  simple_chat:    ttft=2.66s  total=2.72s   tok=4    tps=1.5
  code_gen:       ttft=0.11s  total=2.75s   tok=149  tps=54.2
  reasoning:      ttft=0.10s  total=0.83s   tok=42   tps=50.3
  classification: ttft=0.10s  total=0.17s   tok=5    tps=29.0
  summarization:  ttft=0.11s  total=2.32s   tok=125  tps=53.8

ollama_cloud/gemma4:31b (free tier, network latency included):
  simple_chat:    ttft=0.34s  total=0.43s   ~2 chunks
  code_gen:       ttft=0.35s  total=0.77s   ~18 chunks
  reasoning:      ttft=0.31s  total=0.79s   ~22 chunks
  classification: ttft=0.33s  total=0.41s   ~2 chunks
  summarization:  ttft=0.42s  total=0.67s   ~8 chunks
```
