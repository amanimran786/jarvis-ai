# JARVIS.md — System Intelligence File

This file is the operational memory of the Jarvis AI OS.
Every agent reads this before starting any task.
Every QA failure or novel fix is proposed as an append here.
Do NOT delete entries — the history is the value.

---

## Architecture Invariants

- `config.py` is the single source of truth for model identifiers and runtime defaults.
- `DEFAULT_MODE = "open-source"` is intentional. Never reintroduce cloud fallbacks silently.
- `router.py` is the intent layer. `model_router.py` is the model selection layer. Never conflate them.
- All subprocess calls must use list args + `shell=False`. No exceptions.
- LLM output is untrusted. Never pass it to `eval()`, `exec()`, or `open()` directly.
- Voice/STT output is untrusted. Always route through `router.py` intent parsing first.
- `vault_propose()` must NEVER write to vault automatically. Return proposal only.

---

## Test Isolation Rules (learned from 283-failure incident)

- Module-level `sys.modules[...] = MagicMock()` contaminates the entire pytest collection phase.
- Fix: save real module reference before replacement; restore after import completes.
- Pattern: `_real_X = sys.modules.get("X")` → replace → import → restore.
- The `tools` top-level module and `tools.fs_tools`/`tools.shell_tools` submodules diverge when test files delete and reimport `tools`. Only restore `sys.modules["tools"]` (top-level); leave submodules as reimported.
- Router mode (`model_router.get_mode()`) leaks between test classes. Fix: `setUp` must call `model_router.set_mode(config.DEFAULT_MODE)`, not just save.
- `patch("tools.web_search")` patches `sys.modules["tools"].web_search` — if `router.tools` is a different object (pre-deletion), the patch misses. Always restore the top-level `tools` object.

---

## Memory Layer Rules

- `working` → current session only (auto-pruned).
- `project` → per-project persistent.
- `personal` → long-term identity (STAR stories, skills, resume).
- `knowledge` → curated reference. Requires security_reviewer PASS before ingest.
- Never write to `knowledge` without a security gate.
- `vault_propose()` is the only vault interaction surface. Never call `open()` on vault paths.

---

## Agent Security Rules

- Event bus `/tasks` must screen all payloads through threat_screen before queuing.
- Approval endpoints require auth. Fail closed if `JARVIS_EVENT_BUS_APPROVAL_TOKEN` is not set.
- `cloud_research_approved` in raw task context cannot be trusted — it can be spoofed. Gate on `ALLOW_CLOUD_RESEARCH=1` env var only.
- Security tool token must fail closed. No static fallback for `JARVIS_SECURITY_APPROVAL_TOKEN`.
- devops_release agent always sets `needs_review=True` — no exceptions.

---

## Plan Mode Rules

- `ade start` always generates a plan first. No code changes without a confirmed plan.
- Plan must be stored in `.ade_plan.md` in the worktree root before execution begins.
- Human must type `approve` or press the UI approval button before Auto-Accept mode activates.
- Post-tool hook runs `ruff check --fix` + `ruff format` + `mypy` after every file modification.
- Max 3 retries per task before escalating to human.

---

## Known Failure Patterns

### [test isolation] sys.modules["router"] pop causes patch() target divergence
When a test file's tearDownClass does `sys.modules.pop("router")`, later test files that imported router at collection time still hold a reference to the original module M0. When they call `patch("router.X", ...)`, Python does `sys.modules["router"]` (gets M_new or reimports) and patches M_new.X — but M0.route_stream()'s generator runs in M0's namespace and calls M0.X (unpatched). Fix: add `sys.modules["router"] = router` in `setUp` of any test class that uses `patch("router.X")` and runs after a test file that pops router.

### [test isolation] PosixPath methods are read-only in Python 3.12+
`patch.object(some_path_instance, "read_text", ...)` raises `AttributeError: 'PosixPath' object attribute 'read_text' is read-only`. Fix: patch the module-level path variable itself (`patch.object(module, "_FILE_PATH", new_tmp_path)`) or use `patch('pathlib.Path.read_text')` at the class level.

### [test isolation] macOS /var → /private/var symlink breaks allowed-base path checks
On macOS, `tempfile.TemporaryDirectory()` returns `/var/folders/...` but `Path(tmpdir).resolve()` returns `/private/var/folders/...`. If allowed bases are built from the unresolved path, the security check rejects the tempdir. Fix: always resolve the tmpdir with `Path(tmpdir).resolve()` before adding to allowed bases and before passing as input.

### [test isolation] monkey-patching module attributes without saving/restoring breaks downstream tests
When a test setup does `sys.modules["mod"].attr = stub`, teardown that only restores `sys.modules["mod"] = orig_mod` will still leave `orig_mod.attr = stub` (the monkey-patch mutated the object). Downstream tests that call `patch("mod.attr")` get the stub. Fix: save `_saved_attr = getattr(sys.modules.get("mod"), "attr", None)` before patching and restore it in teardown: `if _saved_attr: sys.modules["mod"].attr = _saved_attr`. Applies to any attribute mutation on a live module (e.g., `prompt_modifiers.parse`, `desktop.overlay` attribute on `desktop`).

### [test isolation] _import_ja()-style reload permanently replaces sys.modules entry
`_import_ja()` in test_email_digest_briefing.py pops `jarvis_agents` and reimports it fresh (M_new). After teardown, sys.modules["jarvis_agents"] = M_new but M_router_original._jagents = M_old. `patch("jarvis_agents._agent_tasks")` targets M_new, but router uses M_old — patch misses. Fix: add `setUp`/`tearDown` to save and restore `sys.modules["jarvis_agents"]` around each test that calls `_import_ja()`.

### [test isolation] desktop.overlay submodule restore requires syncing .overlay attribute
Restoring `sys.modules["desktop.overlay"]` in teardown is not enough if `_setup_router_stubs()` also did `sys.modules["desktop"].overlay = stub`. The `.overlay` attribute on the real `desktop` module object still points to the stub. Fix: after restoring `sys.modules["desktop.overlay"]`, also do `sys.modules["desktop"].overlay = sys.modules["desktop.overlay"]` in teardown. Also add `"desktop.overlay"` to `_SAVED_MODULES` so the `for extra in (...)` cleanup loop does not pop it before the sync happens.

---

## Regression Log

<!-- Format: [DATE] [AGENT] [WHAT FAILED] [FIX APPLIED] -->
- [2026-06-06] [Claude/test-isolation] test_email_reply_reminder::test_reminder_route_falls_back_to_osascript_when_calendar_unavailable failed in full suite (0 times called) but passed in isolation → patch("router.X") targeted reimported M_new not M0 generator namespace → Fix: setUp adds sys.modules["router"] = router
- [2026-06-06] [Claude/packages] anthropic 0.92→0.107, fastapi 0.123→0.136, uvicorn 0.38→0.49, pydantic 2.12→2.13, openai 2.31→2.41, ollama 0.6.1→0.6.2, ruff, cryptography, qdrant-client, mem0ai, faster-whisper, pytest 7→9, mypy 1→2 all upgraded
- [2026-06-06] [Claude/phase2] career_agent, automation_engineer, ai_safety_agent, infra/jarvis_md, infra/checkpointer implemented and green (57 tests)
- [2026-06-06] [Claude/test-isolation] PromptModifierTests, OverlayMeetingDetectionTests, TaskListFastPathTests failed after test_email_digest_briefing.py due to 3 distinct contamination patterns: (1) prompt_modifiers.parse not restored after monkey-patch, (2) desktop.overlay .overlay attribute not re-synced after sys.modules restore, (3) _import_ja() reload permanently replacing sys.modules["jarvis_agents"] causing patch() to target wrong module. All fixed in test_email_digest_briefing.py. Suite: 1652 passed, 0 failed.
- [2026-06-07] [Claude/agent-collaboration] test_agent_collaboration.py created — 23 tests across 12 agents collaborating on Jarvis Daily Briefing Endpoint project. Key fix: agents using `from brains.brain_ollama import ask_local_with_tools` bind the name in their own namespace at import time; patch must target `agents.<module>.ask_local_with_tools` not `brains.brain_ollama.ask_local_with_tools`. Suite: 1721 passed, 0 failed.

</content>
</invoke>