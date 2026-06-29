# CAPABILITIES_AUDIT.md

**Date:** 2026-06-25  
**Auditor:** Senior Engineer audit pass — read from source, not from docs  
**Scope:** router.py (5127L), orchestrator.py, config.py, tool_registry.py, operative.py, task_planner.py, execution_engine.py, voice.py, memory.py, memory_layer.py, agent_dispatch.py, tools/__init__.py, harness/budget.py

---

## 1. Intent Routing

| Capability | Status | Notes |
|---|---|---|
| Polite-prefix stripping ("Jarvis, can you...") | ✅ Working | Compiled regex at import |
| Fast-path regex dispatch (timer, volume, screenshot, etc.) | ✅ Working | ~40 patterns, zero-latency |
| Haiku LLM orchestrator (cloud mode) | ✅ Working | ~300ms, JSON structured output |
| Local structured classifier (Ollama, open-source mode) | ⚠️ Partial | Falls back to FALLBACK="chat" if local model rejects schema |
| Multi-intent detection ("X and Y") | ⚠️ Partial | Only splits on "and", misses "then", "after that" |
| Prompt modifier parsing | ✅ Working | Strips system-extra hints from user text |

---

## 2. Core System Tools

| Capability | Status | Notes |
|---|---|---|
| App launcher (macOS `open -a`) | ✅ Working | |
| Volume control (osascript) | ✅ Working | |
| Brightness control (`brightness` CLI) | ⚠️ Partial | Requires `brightness` brew package |
| Screenshot (`screencapture`) | ✅ Working | |
| Lock screen | ✅ Working | |
| Clipboard read/write | ✅ Working | |
| Battery status (`pmset`) | ✅ Working | |
| Math evaluator | ✅ Working | Simple arithmetic + unit conversion baked in |
| Timer/countdown (osascript alarm) | ✅ Working | |
| Shell command execution (`terminal.run_command`) | ✅ Working | No sandbox — full host access |
| Admin/sudo execution | ✅ Working | No confirmation gate — risky |

---

## 3. Communication

| Capability | Status | Notes |
|---|---|---|
| iMessage compose + send | ⚠️ Partial | Requires Accessibility permission; multi-contact disambiguation logic exists and is complex |
| iMessage thread read | ⚠️ Partial | `messages.py` exists; depends on Messages DB permission |
| Gmail read (inbox/unread) | ⚠️ Partial | OAuth via `google_services.py`; needs active token |
| Gmail compose + send | ⚠️ Partial | Confirmation gate works; token expiry breaks silently |
| Gmail search | ⚠️ Partial | `_parse_email_search_query` present |
| Gmail reply | ⚠️ Partial | `_find_email_to_reply` + pending-reply state machine |
| Contact lookup/resolution | ⚠️ Partial | Fuzzy match against `contact_aliases.json`; breaks on ambiguous names |

---

## 4. Calendar

| Capability | Status | Notes |
|---|---|---|
| Read today's events (Google Cal) | ⚠️ Partial | OAuth-dependent; token refresh not automatic |
| Create event / reminder | ⚠️ Partial | `gs.create_event()` + osascript fallback |
| Natural language time parsing (3pm, tomorrow at 10) | ✅ Working | Handles AM/PM, next-day rollover |
| Week-ahead summary | ✅ Working | Via `_jagents.week_ahead()` |
| Meeting prep briefing | ✅ Working | Pulls next event + memory context |

---

## 5. Web & Research

| Capability | Status | Notes |
|---|---|---|
| Web search (DuckDuckGo via `ddgs`) | ✅ Working | Summarized via local Ollama |
| Weather (wttr.in) | ✅ Working | JSON API, no key needed |
| Browser open/navigate (subprocess `open`) | ✅ Working | |
| Browser page summarize (CDP) | ⚠️ Partial | `local_runtime.local_browser.fetch_page()` exists; CDP connection fragile |
| Browser click (AppleScript) | ⚠️ Partial | `browser.open_then_click()` exists; brittle on dynamic pages |
| Deep research (multi-step web + synthesis) | ⚠️ Partial | `research.py` exists; calls cloud Sonnet; slow on local |
| Meeting captions read (browser DOM) | ⚠️ Partial | Google Meet-specific selectors |

---

## 6. Memory

| Capability | Status | Notes |
|---|---|---|
| Working memory (facts/prefs/projects, JSON file) | ✅ Working | Persists across sessions |
| Conversation history (last 8 turns, JSON) | ✅ Working | Capped at `MAX_CONVERSATION_TURNS=8` |
| TF-IDF semantic memory (`semantic_memory.py`) | ✅ Working | In-process; no external dependency |
| mem0 episodic memory | ⚠️ Partial | `mem0_layer.py` exists; async writes after each turn; quality depends on local extraction |
| Memory consolidation command | ✅ Working | `/consolidate memory` |
| Memory recall in context | ⚠️ Partial | `memory_layer.runtime_context()` builds a context block but it's not always prepended automatically |
| Forget / remove fact | ✅ Working | `memory.forget(keyword)` |

---

## 7. Knowledge Vault (Obsidian)

| Capability | Status | Notes |
|---|---|---|
| Read vault notes | ✅ Working | `vault.py` reads markdown files |
| Write/capture to vault | ✅ Working | `vault_capture.handle_capture()` |
| Background vault agent tasks | ⚠️ Partial | `task_runtime.submit_task()` submits; execution depends on harness/loop.py running |
| Source ingest (URLs, repos) | ⚠️ Partial | `source_ingest.py` exists; chunking + indexing pipeline |
| Daily note creation | ✅ Working | Pulls calendar + tasks + focus into Obsidian note |
| Vault wiki builder | ⚠️ Partial | `wiki_builder.py` exists; slow on large vaults |

---

## 8. Voice / Ambient

| Capability | Status | Notes |
|---|---|---|
| Wake-word detection ("Jarvis", "Hey Jarvis") | ✅ Working | String match on STT output; not keyword-model |
| STT: faster-whisper (local) | ✅ Working | large-v3-turbo default; VAD filter; CPU int8 |
| STT: OpenAI Whisper (cloud fallback) | ✅ Working | |
| TTS: macOS `say` (local) | ✅ Working | Daniel voice, 168 WPM |
| TTS: Kokoro (local neural) | ⚠️ Partial | `local_kokoro_tts.py` exists; subprocess bridge sometimes unstable |
| TTS: ElevenLabs (cloud) | ✅ Working | Requires API key |
| TTS: OpenAI TTS (cloud) | ✅ Working | |
| Mic device selection / fallback | ✅ Working | Prefers MacBook mic over virtual devices |
| Call privacy / mute during meetings | ✅ Working | `call_privacy.py` |
| Meeting audio listener | ⚠️ Partial | `meeting_listener.py`; real-time transcript but no persistent summaries |

---

## 9. Autonomous / Agentic

| Capability | Status | Notes |
|---|---|---|
| Operative (multi-step task execution) | ⚠️ Partial | `operative.py` → `task_planner.py` (Sonnet) → `execution_engine.py`; cloud-required for planning; no loop-back on failure |
| Task planner (plan_task) | ⚠️ Partial | Uses Sonnet; local path not tested |
| Execution engine (step execution + verifier) | ✅ Working | Resolves `$step_N_result` references; runs tools |
| Background task queue (task_runtime.py) | ⚠️ Partial | SQLite-backed; threading semaphore at 1; watchdog exists |
| Approval gate for side-effects | ✅ Working | Confidence < 0.74 → human review |
| jarvis_executor multi-step (`_jexec`) | ⚠️ Partial | Detects compound commands; delegates to `operative` |
| Specialized agents (planner/executor/reviewer) | ⚠️ Partial | `specialized_agents.py` calls them sequentially; 3–5 LLM round-trips; slow locally |
| Agent dispatch (Ollama tool-calling loop) | ⚠️ Partial | `agent_dispatch.py` with 8 named agents; event bus not running in prod |
| Agent worker (event bus polling) | ❌ Missing | `agent_worker.py` polls `localhost:8766`; no event bus running |
| Redis Streams coordination | ❌ Missing | Architecture doc specifies it; `docker-compose.yml` exists; not wired into Python |
| Code sandbox (safe execution) | ❌ Missing | Shell runs on host; no container/namespace isolation |

---

## 10. Code Intelligence

| Capability | Status | Notes |
|---|---|---|
| Run shell commands | ✅ Working | `terminal.run_command()` |
| Write files to disk | ✅ Working | `tools/fs_tools.py` |
| Read files from disk | ✅ Working | |
| Self-modify Jarvis source code | ⚠️ Partial | `self_improve.py` with diff/backup/approval gate; no test-run after patch |
| Coder workbench (repo-grounded coding status) | ✅ Working | Git diff + status surface |
| Write code + run tests | ❌ Missing | No test-run-fix loop; no iterative code agent |
| Code execution sandbox | ❌ Missing | |
| Git operations (commit, branch, PR) | ❌ Missing | Only `git diff` / `git status` read |
| Diff-aware patching | ⚠️ Partial | `self_improve.py` patches one file; no multi-file awareness |

---

## 11. OSINT / Security

| Capability | Status | Notes |
|---|---|---|
| Username footprint (Maigret) | ⚠️ Partial | Requires `maigret` installed |
| Domain typo-squatting (DNSTwist) | ⚠️ Partial | Requires `dnstwist` installed |
| Subdomain enumeration (subfinder) | ⚠️ Partial | Requires `subfinder` installed |
| WHOIS lookup | ⚠️ Partial | Uses python-whois |
| Security reviewer agent | ✅ Working | LLM-based; no live scanner |
| Prompt injection defense | ✅ Working | `security_roe.py` + identity override guards in router |

---

## 12. Self-Improvement / Eval

| Capability | Status | Notes |
|---|---|---|
| Self-review (`/score`, `/reflect`, `/diagnose`) | ✅ Working | Reads from `self_eval_log.py` |
| Self-improve (propose + approval gate + apply) | ⚠️ Partial | One-file patches; no post-apply test run |
| Local model training (MLX LoRA) | ⚠️ Partial | `local_mlx_training.py`; requires Apple Silicon + `mlx-tune` |
| Distillation from failures | ⚠️ Partial | `local_training.export_sft_dataset()` |
| Preference dataset export (DPO) | ⚠️ Partial | `local_training.export_preference_dataset()` |
| Local model eval + promote | ⚠️ Partial | `local_model_eval.run_eval()` |
| Model benchmark | ⚠️ Partial | `local_model_benchmark.run_benchmark()` |
| Budget tracking (token/cost) | ✅ Working | `harness/budget.py`; soft + hard limits per provider |
| Usage log | ✅ Working | `usage_log.jsonl` |

---

## 13. Infrastructure

| Capability | Status | Notes |
|---|---|---|
| PyQt6 desktop UI | ✅ Working | `ui.py` + `main.py` |
| FastAPI REST API (`api.py`) | ⚠️ Partial | Exists; not documented as primary channel |
| Headless CLI mode | ✅ Working | `python main.py --no-ui` |
| Packaged app (PyInstaller) | ⚠️ Partial | `Jarvis.spec` exists; packaged path sometimes breaks local imports |
| Runtime state (data paths) | ✅ Working | `runtime_state.py` handles both dev and packaged paths |
| Audit log | ✅ Working | `harness/audit.py` |
| Provider priority routing | ✅ Working | `provider_router.py`; local → ollama-cloud → paid |
| Kimi K2.7 coder (via OpenRouter) | ⚠️ Partial | Config exists; `JARVIS_KIMI_ENABLED=false` default; no evals done |
| Apple Foundation model | ⚠️ Partial | Config exists; `JARVIS_APPLE_FOUNDATION_ENABLED=false` default |
| Local artifacts (HTML generation) | ✅ Working | Saves interactive HTML to Desktop |

---

## Summary Counts

| Status | Count |
|---|---|
| ✅ Working | ~35 |
| ⚠️ Partial | ~45 |
| ❌ Missing | ~5 |

The core single-turn loop (voice → route → tool → speak) works. The agentic layer (multi-step, background agents, code iteration) is scaffolded but not production-reliable.
