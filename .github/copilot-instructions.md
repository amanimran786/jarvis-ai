# Jarvis — GitHub Copilot Instructions

This is the Jarvis AI codebase: a local-first macOS desktop intelligence runtime (Python 3.10+, PyQt6, Ollama, FastAPI). These instructions keep Copilot in sync with the same context used by Claude Code and Codex on this project.

## Shared Brain

The Obsidian vault at `vault/wiki/brain/` is the durable operational memory shared across all AI coding tools. Before making architectural decisions, check:
- `vault/wiki/brain/03 Brain Schema.md` — metadata and linking rules
- `vault/wiki/brain/04 Capture Workflow.md` — promotion and placement rules
- `vault/indexes/Repo Map.md` — current repo map

Never auto-write to the vault. Propose changes and wait for approval.

## Core Principles (same as CLAUDE.md)

**Think before coding.** State assumptions. Ask one focused question if ambiguous. Follow repo patterns over inventing new ones.

**Smallest correct change.** No speculative abstractions. No future-proofing. Would a strong engineer call it tight and boring?

**Surgical.** Touch only what the request requires. No adjacent refactors. No symbol renames for preference.

**Goal-driven.** Define success condition → make change → verify with narrowest check. For voice/UI/packaging changes, verify the packaged app too.

## Local-First Rules

- `config.py` is the source of truth for all runtime defaults. Change defaults there, not inline.
- `DEFAULT_MODE = "open-source"` is intentional. Do not reintroduce paid/cloud fallbacks.
- If a local path fails, fix the local path first.

## Repo Structure

```
main.py              — entry point (--no-ui for headless)
router.py            — intent/tool routing before LLM
model_router.py      — model selection and mode behavior
orchestrator.py      — request/runtime coordination
config.py            — all runtime defaults
voice.py             — voice loop, wake/listen/TTS
ui.py                — PyQt6 desktop app
local_runtime/       — local STT, TTS, Kokoro, oMLX, CocoIndex, browser
brains/              — Claude, Gemini, Ollama, GPT backends
memory.py            — persistent memory
learner.py           — knowledge feed and behavioral insights
usage_tracker.py     — token and cost tracking
tests/               — pytest suite (563+ tests)
vault/               — Obsidian brain (read carefully before writing)
```

## Security Rules (before every commit)

```bash
python -m harness.pre_commit_check
```

(replaces the old manual greps — the checker runs all of them plus py_compile
and affected tests in one pass. Required before every commit.)

- No hardcoded secrets — use `os.getenv()`
- All subprocess calls: list args, `shell=False`
- User-controlled paths: `Path(p).resolve()`, reject `..`
- LLM/voice output: never pass to `eval()`, `exec()`, or `open()` directly
- Voice/STT input is untrusted — always route through `router.py` first

## Testing Rules

Run narrowest test first:
```bash
python3 -m pytest tests/test_<specific>.py -q
python3 -m pytest tests/ -q --tb=short 2>&1 | tail -10
```

Mock pattern for PyQt6/sounddevice (sandbox only):
```python
import sys
from unittest.mock import MagicMock
sys.modules['PyQt6'] = MagicMock()
sys.modules['sounddevice'] = MagicMock()
```

AAA pattern: Arrange → Act → Assert. One concern per test. Name: `test_returns_empty_when_no_match`.

Logic/config change → unit test. UI regression → regression test. Packaged fix → test + rebuild.

## Voice Domain (see .claude/skills/jarvis-voice.md)

Check in order: mic open → device selected → audio captured → STT model loaded → packaged assets present → UI status driven by real state.

Runtime logs: `~/Library/Application Support/Jarvis/.jarvis_voice.log`

## Packaging Domain (see .claude/skills/jarvis-packaging.md)

Any change to `voice.py`, `ui.py`, `main.py`, `Jarvis.spec`, or `local_runtime/` requires packaged app verification:
```bash
scripts/install_jarvis_app.sh --applications-only
```
No `print()` in windowed modules — use `logging`.

## Key Agents Available (invoke in Claude Code or Codex)

- `python-reviewer` — PEP 8, type hints, Jarvis-specific security patterns
- `security-reviewer` — injection, path traversal, secrets, LLM output safety
- `tdd-guide` — write-tests-first, pytest red-green-refactor
- `silent-failure-hunter` — swallowed exceptions, missing logging
- `build-error-resolver` — pytest/PyInstaller failures, minimal diffs only

## Token/Cost Discipline

This codebase tracks token usage in `usage_log.jsonl`. Fixed prompt overhead is ~1,316 tokens. Keep changes tight. Avoid bulk file reads unless necessary.

## Communication Style

- Short plans with explicit assumptions
- Exact file paths and commands
- Absolute timestamps for builds/installed apps
- If uncertain: say what is known and what is not

---

## Parallel Work with Claude (Cowork)

CI is green as of commit `82b7804`. Claude (Cowork) and Codex work in parallel
on separate roadmap items. See `CROSS_AGENT_ORCHESTRATION.md` for the full
protocol. Key rules:

**Codex lane (Items 3, 5, 7, 8):**
- Item 3 — Orchestrator self-healing (launchd KeepAlive for `orchestrator_loop.py`)
- Item 5 — Specialist model routing (devstral + qwen3:30b-a3b in Ollama)
- Item 7 — Test coverage hardening (every harness module has an import-smoke test)
- Item 8 — Voice pipeline end-to-end (Kokoro/Whisper/say fallback, no zombie mics)

**Branch convention:** `codex/roadmap-N-short-name`, off `main`.

**Merge rule:** CI must be green before any branch merges to `main`. Push, wait
for the `CI / Test suite` check, then merge.

**Shared files:** If you need to touch `config.py`, `orchestrator.py`, or
`router.py`, check git log first — Claude may have modified them. Rebase before
editing.
