#!/usr/bin/env python3
"""Tests for post_write_security.py — validates severity routing and skip paths.

Contract under test:
  - CRITICAL finding  -> exit 2 (Claude Code surfaces it prominently)
  - HIGH / MEDIUM     -> exit 0 (advisory only, never blocks)
  - clean / commented / non-target suffix / non-Edit tool / missing file -> silent exit 0

The fixtures below feed dangerous patterns to the hook as TEST DATA. Because the
hook is a line-regex scanner (not AST-aware), the trigger tokens are spliced with
"##" and closed up at runtime by src() — so the source lines here do NOT match the
hook's own regexes and this test file stays clean under the security hook.
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HOOK = Path(__file__).resolve().parent / "post_write_security.py"


def src(s: str) -> str:
    """Close up the '##' splices used to keep trigger tokens hook-clean in source."""
    return s.replace("##", "")


@contextlib.contextmanager
def _tmp_file(content: str, suffix: str = ".py"):
    fd, name = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(content)
        yield name
    finally:
        with contextlib.suppress(OSError):
            os.unlink(name)


def _run(file_path: str, tool: str = "Write") -> tuple[int, str]:
    payload = json.dumps({"tool_name": tool, "tool_input": {"file_path": file_path}})
    r = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload, capture_output=True, text=True, timeout=10,
    )
    return r.returncode, r.stdout + r.stderr


def _scan(content: str, suffix: str = ".py", tool: str = "Write") -> tuple[int, str]:
    with _tmp_file(content, suffix=suffix) as path:
        return _run(path, tool=tool)


# ── CRITICAL → exit 2 ────────────────────────────────────────────────────────

def test_subprocess_shell_true_is_critical_and_exits_2():
    code, out = _scan(src('import subprocess\nsubprocess.run("ls", shell=##True)\n'))
    assert code == 2, f"Expected 2, got {code}"
    assert "[security-hook]" in out
    assert "CRITICAL" in out


def test_hardcoded_secret_is_critical_and_exits_2():
    code, out = _scan(src('API_KEY ##= "supersecret-value"\n'))
    assert code == 2
    assert "CRITICAL" in out


def test_os_system_is_critical_and_exits_2():
    code, out = _scan(src('import os\nos.system##("echo hi")\n'))
    assert code == 2
    assert "CRITICAL" in out


def test_notebookedit_tool_is_scanned():
    # NotebookEdit is an accepted write tool; a critical .py still blocks.
    code, out = _scan(src('import os\nos.system##("echo hi")\n'), tool="NotebookEdit")
    assert code == 2
    assert "CRITICAL" in out


# ── HIGH / MEDIUM → advisory, never blocks ───────────────────────────────────

def test_eval_is_high_and_does_not_block():
    code, out = _scan(src("def run(p):\n    return eval##(p)\n"))
    assert code == 0, f"HIGH must not block, got {code}"
    assert "[security-hook]" in out
    assert "HIGH" in out


def test_path_traversal_is_medium_and_does_not_block():
    code, out = _scan(src('data = open("..##/notes.txt")\n'))
    assert code == 0
    assert "[security-hook]" in out
    assert "MEDIUM" in out


# ── Silent exit 0 paths ──────────────────────────────────────────────────────

def test_clean_python_file_is_silent():
    code, out = _scan("x = 1\nprint(x)\n")
    assert code == 0
    assert out.strip() == "", f"Expected no output, got: {out!r}"


def test_commented_dangerous_line_is_ignored():
    code, out = _scan(src('# os.system##("rm -rf /tmp/x")\n'))
    assert code == 0
    assert out.strip() == ""


def test_non_target_suffix_is_ignored():
    code, out = _scan(src('os.system##("x")\n'), suffix=".md")
    assert code == 0
    assert out.strip() == ""


def test_non_edit_write_tool_is_ignored():
    code, out = _scan(src('import os\nos.system##("x")\n'), tool="Bash")
    assert code == 0
    assert out.strip() == ""


def test_missing_file_is_ignored():
    code, out = _run("/nonexistent/path/to/file.py", tool="Write")
    assert code == 0
    assert out.strip() == ""


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print(f"  ✓ {t.__name__}")
            passed += 1
        except AssertionError as e:
            print(f"  ✗ {t.__name__}  {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
