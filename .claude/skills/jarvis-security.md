# Jarvis Security Rules

## Before Every Commit

Run these checks on any new Python file:

```bash
grep -n "shell=True" <file>.py
grep -n "eval\|exec(" <file>.py
grep -n "SECRET\|API_KEY\|TOKEN\|PASSWORD" <file>.py | grep -v "os.getenv\|config."
grep -n "pickle.load\|yaml.load" <file>.py
```

If any match: stop and fix before committing.

## Mandatory Pre-Commit Checklist

- [ ] No hardcoded secrets (API keys, passwords, tokens) — use `os.getenv()`
- [ ] All subprocess calls use list args, `shell=False`
- [ ] User-controlled paths validated with `Path.resolve()`, `..` rejected
- [ ] LLM/voice output never passed to `eval()`, `exec()`, or `open()` directly
- [ ] Exception handlers log with `logging.exception()`, not `print()`
- [ ] `.env` file is in `.gitignore`
- [ ] No API keys in log output

## Jarvis-Specific Security Rules

### Voice Input Is Untrusted
STT output is user-controlled text. Never pass it directly to:
- `subprocess.run()` or `os.system()`
- `eval()` or `exec()`
- File paths without validation

Always route through `router.py` intent parsing first.

### LLM Output Is Untrusted
Model responses can contain anything. Never:
- `eval()` or `exec()` LLM output
- Use LLM-generated file paths without validation
- Trust LLM-generated JSON schema without validation

### Subprocess Safety Pattern
```python
# WRONG
subprocess.run(user_input, shell=True)

# RIGHT
subprocess.run(["say", validated_text], shell=False, timeout=30)
```

### File Path Safety Pattern
```python
from pathlib import Path
ALLOWED_BASE = Path("/Users/truthseeker/jarvis-ai")

def safe_path(user_input: str) -> Path:
    p = (ALLOWED_BASE / user_input).resolve()
    if not str(p).startswith(str(ALLOWED_BASE)):
        raise ValueError(f"Path traversal rejected: {p}")
    return p
```

## Security Response Protocol

If a security issue is found:
1. STOP — do not commit
2. Use the **security-reviewer** agent for full audit
3. Fix CRITICAL issues before any other work
4. Rotate any exposed secrets immediately
5. Search entire codebase for similar patterns
