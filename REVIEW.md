# REVIEW.md — Pre-Commit Gate

Every code session **must** run this checklist on every changed `.py` file before
committing. Not "should" — must. If any check fails: stop, fix, re-run.

This is enforced infrastructure. Future work will wire it as a TASK_CONTRACTS.json
precondition. Until then, treat it as a hard contract enforced by the session itself.

---

## Step 1 — Identify changed files

```bash
# List Python files staged for commit (or changed since last commit)
git diff --name-only HEAD -- '*.py'
```

Record the list. Every subsequent step runs against **this list only**, not the whole
repo. If the list is empty, skip to Step 7.

---

## Step 2 — Security scan (automated, zero-tolerance)

Run each grep. **Any match = stop and fix before continuing.**

```bash
# 2a. shell=True in subprocess (RCE risk)
grep -n "shell=True" <changed_files>

# 2b. eval / exec (arbitrary code execution)
grep -n "eval\|exec(" <changed_files>

# 2c. Hardcoded secrets (not via os.getenv or config.)
grep -n "SECRET\|API_KEY\|TOKEN\|PASSWORD" <changed_files> | grep -v "os\.getenv\|config\."

# 2d. Unsafe deserialization
grep -n "pickle\.load\|yaml\.load" <changed_files>
```

Each grep should return zero matches. Document which files you checked.

---

## Step 3 — Syntax check (automated)

```bash
python -m py_compile <each_changed_file>
```

All files must compile cleanly. Fix any `SyntaxError` before continuing.

---

## Step 4 — Manual security checklist (judgment-based)

For each changed file, confirm:

- [ ] **No hardcoded secrets** — all credentials via `os.getenv()` or `config.`
- [ ] **Subprocess safety** — all `subprocess` calls use list args, `shell=False`, `timeout` set
- [ ] **Path validation** — user-controlled paths go through `Path.resolve()` and a base-dir check
- [ ] **STT/LLM output never reaches** `eval()`, `exec()`, or an unvalidated `open()` call
- [ ] **Exceptions** — handlers use `logging.exception()`, not bare `print()`
- [ ] **No secrets in log output** — `log.info(...)` lines don't echo API keys or tokens
- [ ] **`.env` in `.gitignore`** — confirm before first commit in any new session

Reference patterns if needed: `.claude/skills/jarvis-security.md`

---

## Step 5 — Run affected tests

```bash
# Run the test module(s) that cover the changed files.
# Minimum: import-smoke test for the changed module.
python -m pytest tests/ -x -q --tb=short -k "<relevant_keyword>"
```

All touched test modules must pass. If no test exists for a changed module, note it
and create a minimal import test (`import <module>`) before committing.

---

## Step 6 — Commit via git plumbing (FUSE mount requirement)

**Do not use `git commit` directly** — the FUSE mount blocks `.git/index.lock` removal
and causes hangs. Use the plumbing pattern:

```bash
# Stage via temp index
export GIT_INDEX_FILE=/tmp/work_$(date +%s).idx
cp .git/index "$GIT_INDEX_FILE"
git update-index --add <changed_files>
TREE=$(git write-tree)
PARENT=$(git rev-parse HEAD)
COMMIT=$(git commit-tree "$TREE" -p "$PARENT" -m "your message here")

# Advance HEAD (Python write avoids lock contention)
python3 -c "
from pathlib import Path
Path('.git/refs/heads/$(git rev-parse --abbrev-ref HEAD)').write_text('$COMMIT\n')
"
unset GIT_INDEX_FILE
```

Then verify:
```bash
git log --oneline -3
git show --stat HEAD
```

---

## Step 7 — Sign-off

Paste this block at the bottom of your task completion note:

```
REVIEW.md GATE: PASSED
Checked files: <list>
Security scan: clean
py_compile: clean
Tests: <N> passed
Commit: <sha>
```

If any item was waived (e.g., no tests exist yet), note the waiver and the follow-up
task ID that will close the gap.

---

## What gets checked here vs. elsewhere

| Concern | Here | jarvis-security.md |
|---|---|---|
| Runnable grep commands | ✅ | advisory only |
| py_compile gate | ✅ | not covered |
| Test run requirement | ✅ | not covered |
| Git plumbing reminder | ✅ | not covered |
| Patterns / examples | — | ✅ |
| Security response protocol | — | ✅ |

`jarvis-security.md` remains the authoritative reference for *why* each rule exists.
`REVIEW.md` is the runnable gate you execute before every commit.
