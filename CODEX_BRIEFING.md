# Jarvis AI — Codex Session Briefing

> Read this at the start of every session. It is the ground truth for project state,
> conventions, and your current task queue.

---

## What Jarvis Is

Jarvis is a **local-first macOS autonomous AI runtime**. Its purpose:

- Run continuously as a background agent, processing tasks from a work queue every 5 minutes
- Handle voice input/output, meetings, memory, calendar, and tool execution locally
- Use cloud LLMs (OpenAI, Anthropic, Gemini) only as fallbacks — never as the primary path
- Expose a dashboard at `http://localhost:7842` and a REST API for mobile/remote access
- Execute tasks autonomously from `WORK_QUEUE.json` without human intervention

The guiding principle: **local-first, always on, never blocked by rate limits**.

---

## Project State (as of 2026-07-09)

### Infrastructure — completed
| Area | Status | Notes |
|---|---|---|
| Local LLM routing | ✅ Done | 97%+ traffic hits Ollama/GLM-4.7-flash |
| Rate limit backoff | ✅ Done | `brains/_retry.py` — 2/4/8s backoff, 3 retries |
| Cloud budget cap | ✅ Done | 300k tokens/hr enforced in `model_router.py` |
| Circuit breaker | ✅ Done | `harness/circuit_breaker.py` — OPEN 10min, persists to disk |
| Ollama liveness | ✅ Done | `brains/brain_ollama.py` — 2s check, 30s cache |
| Request queuing | ✅ Done | `harness/request_queue.py` — 60s wait, depth-10 cap |
| Typed task contracts | ✅ Done | `harness/task_contract.py` — gate in `orchestrator_loop.py` |
| Capability checker | ✅ Done | `harness/capability_checker.py` — runtime probes before dispatch |
| Approval workflow | ✅ Done | `harness/approval_workflow.py` — record_approval / list_pending |
| Stale-session expiry | ✅ Done | `orchestrator_loop._expire_stalled_sessions()` — 90min timeout, step 0 of every iteration |
| Dashboard | ✅ Done | `jarvis_dashboard.py` — port 7842, shows queue/provider health |
| Plugin system | ✅ Done | `harness/plugin_loader.py` + `plugins/` |
| History CLI | ✅ Done | `/history` command with Rich tables |
| Cloud bypass | ✅ Done | `research.py` last unconditional cloud call routed to `ask_with_priority` |

### WORK_QUEUE state
```
Total tasks: 95
  done:     74
  queued:   21   ← all have typed contracts, ready to dispatch
  blocked:   0   ← clean! all tasks are contractable
```

### No more stale-session lockouts
The orchestrator loop now auto-expires sessions at step 0 of every iteration.
Sessions silent for 90+ minutes are marked `stalled` and their queue tasks requeued.
You will never need to manually reset `ACTIVE_SESSIONS.json`.

---

## How to Work in This Repo

### Before touching any file
```bash
# See what's already been done
git log --oneline -20

# Run tests to get a baseline
python -m pytest tests/ -x -q --timeout=30 --continue-on-collection-errors 2>&1 | tail -10
```

### Commit convention
```
[CODEX] feat(area): short description of what was built
[CODEX] fix(area): short description of what was fixed
[CODEX] docs(area): documentation changes
[CODEX] tests: what the tests cover
```

Always run `python3 -m py_compile <file>` after editing Python files.

### Testing
```bash
# Core harness tests (always run these)
python -m pytest tests/test_orchestrate.py tests/test_task_contract.py \
  tests/test_circuit_breaker.py tests/test_approval_workflow.py \
  tests/test_session_tracker.py -q --timeout=30

# Expected: 70 passed

# Full suite (some collection errors expected — PyQt6, network, env-gated tests)
python -m pytest tests/ -q --timeout=30 --continue-on-collection-errors 2>&1 | tail -20
```

### Git lock workaround (FUSE mount issue)
If you see `fatal: Unable to create '.git/index.lock': File exists`, use the plumbing
commit path (see `CODEX_TASKS.md` or ask the parent session). Do NOT use `rm` on lock files.

---

## Key Files and What They Do

### Entry points
```
main.py              — GUI mode: python main.py
main.py --no-ui      — Headless mode
jarvis_dashboard.py  — Dashboard: python jarvis_dashboard.py (port 7842)
```

### Core routing
```
router.py            — Intent/tool routing before LLM use
model_router.py      — Model selection, local-first plan building
orchestrator.py      — Request/runtime coordination
operative.py         — Task step execution
```

### Autonomous loop
```
orchestrator_loop.py          — Main 5-min loop, reads WORK_QUEUE.json
harness/cowork_launcher.py    — Fires sessions from LAUNCH_QUEUE.json
harness/runtime_launcher.py   — Executes contracted tasks
harness/task_contract.py      — TaskContract schema + gate logic
harness/session_tracker.py    — ACTIVE_SESSIONS.json management + expire_stalled()
harness/capability_checker.py — Probes capabilities before dispatch
harness/approval_workflow.py  — record_approval(), list_pending_approvals()
```

### LLM infrastructure
```
brains/brain.py               — OpenAI lane (local-first gate at line 85)
brains/brain_claude.py        — Anthropic lane (prompt caching enabled)
brains/brain_gemini.py        — Gemini lane
brains/brain_ollama.py        — Local Ollama lane (liveness check at line 47)
brains/_retry.py              — 429 retry with 2/4/8s backoff
harness/circuit_breaker.py    — Per-provider CLOSED/OPEN/HALF_OPEN states
harness/request_queue.py      — Queue when all providers exhausted
provider_priority.py          — ask_with_priority() — the safe non-streaming path
```

### State files (read/write carefully)
```
WORK_QUEUE.json               — Task queue. Statuses: queued/in_progress/done/blocked/awaiting_approval
TASK_CONTRACTS.json           — Typed contracts for autonomous tasks (22 contracts)
ORCHESTRATOR_STATUS.json      — Live orchestrator state, provider health, queue depth
ACTIVE_SESSIONS.json          — Currently running sessions (auto-expired after 90min)
LAUNCH_QUEUE.json             — Sessions queued for launch
approved_tasks.json           — Human-approved tasks for requires_approval=True contracts
logs/circuit_breaker.json     — Persisted circuit breaker state
```

### Config
```
config.py            — Source of truth for all defaults. Change here, not inline.
.env                 — API keys and runtime overrides (gitignored)
```

---

## How Contracts Work

A **TaskContract** is required before the orchestrator will execute a task autonomously.

```python
from harness.task_contract import (
    TaskContract, load_contracts, save_contracts, validate_contract
)

# Load existing contracts
contracts = load_contracts()

# Build a new one
contract = TaskContract(
    task_id="my-task-slug",          # must match contract_id in WORK_QUEUE entry
    task_type="code",                # "code" | "file_op" | "analysis" | "test"
    description="What this task does in one sentence",
    contract_version="1.0",
    inputs=[{"name": "source_file", "type": "file_path", "required": True,
             "description": "...", "default": ""}],
    outputs=[{"name": "report", "type": "file", "path_template": "logs/report.json",
              "description": "..."}],
    side_effects=["writes_files"],           # "writes_files" | "subprocess" | "network" | "modifies_config"
    requires_capabilities=["filesystem", "python"],  # only valid Capability enum values
    reversible=True,
    requires_approval=False,
    entry_point="python -m pytest tests/ -q",
    working_directory="/Users/truthseeker/jarvis-ai",
    estimated_tokens=3000,
    max_duration_seconds=120,
    preconditions=["source_file exists"],
    postconditions=["logs/report.json exists and is valid JSON"],
)
contracts[contract.task_id] = contract
save_contracts(contracts)
```

**Valid `requires_capabilities` values:**
`ollama`, `filesystem`, `internet`, `git`, `python`, `voice`, `calendar`, `imessage`, `screen`

Note: `subprocess` is a `side_effect`, NOT a capability. Using it in `requires_capabilities` will
cause the loader to silently skip the entire contract entry.

**To unblock a queued task:**
1. Write its contract in `TASK_CONTRACTS.json`
2. Add `contract_id` field to the WORK_QUEUE entry matching your contract's `task_id`
3. Set the entry's `status` to `"queued"`
4. The orchestrator validates and dispatches on next loop iteration

**Validate all contracts:**
```bash
python3 -c "
from harness.task_contract import load_contracts, validate_contract
contracts = load_contracts()
errors = 0
for tid, c in contracts.items():
    ok, errs = validate_contract(c)
    if not ok:
        print(f'INVALID {tid}: {errs}')
        errors += 1
print(f'{len(contracts)} contracts, {errors} invalid')
"
```

---

## Environment

```bash
# Local models
LOCAL_DEFAULT_MODEL=glm-4.7-flash   # via Ollama on localhost:11434
LOCAL_CODER_MODEL=glm-4.7-flash
DEEPSEEK_CTX=32768                  # raised from 8192

# Cloud fallbacks (budget-capped at 300k tokens/hr)
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
GEMINI_API_KEY=...

# Runtime
DEFAULT_MODE=auto                    # local-first, falls back to cloud
JARVIS_LOCAL_STRICT_FIRST=1          # prefer local in all modes
OLLAMA_TIMEOUT_SECONDS=120           # raised from 45

# STT/TTS (fully local)
JARVIS_FASTER_WHISPER_MODEL=large-v3-turbo
JARVIS_KOKORO_TTS_ENABLED=1
```

---

## Domain Rules (read before touching these areas)

- **Voice/STT/TTS/mic:** read `.claude/skills/jarvis-voice.md` first
- **Packaging (PyInstaller):** read `.claude/skills/jarvis-packaging.md` first
- **Obsidian vault:** read `.claude/skills/jarvis-vault.md` first
- **Tests:** read `.claude/skills/jarvis-testing.md` first
- **Security:** all rules in `.claude/skills/jarvis-security.md` — run security grep before every commit

Security pre-commit check:
```bash
grep -n "shell=True" <file>.py
grep -n "eval\|exec(" <file>.py
grep -n "SECRET\|API_KEY\|TOKEN" <file>.py | grep -v "os.getenv\|config\."
```

---

## Current Branch

```
Branch: improve/local-artifact-and-dashboard
~22 commits ahead of origin
```

All infrastructure work lives on this branch.

---

*Last updated: 2026-07-09 | Maintained by: Jarvis AI session tooling*
