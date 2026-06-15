#!/usr/bin/env python3
"""Tests for post_write_lint.py — validates the advisory ruff lint hook.

Contract under test:
  - lint violation -> exit 0 BUT prints a "[lint-hook]" summary (advisory, never blocks)
  - clean file     -> exit 0, silent
  - non-.py suffix / missing file / non-Edit-Write tool -> exit 0, silent
  - both Edit and Write trigger the hook

The hook always exits 0; its only observable effect is the printed summary.
"""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HOOK = Path(__file__).resolve().parent / "post_write_lint.py"


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
        input=payload, capture_output=True, text=True, timeout=20,
    )
    return r.returncode, r.stdout + r.stderr


def _lint(content: str, suffix: str = ".py", tool: str = "Write") -> tuple[int, str]:
    with _tmp_file(content, suffix=suffix) as path:
        return _run(path, tool=tool)


# ── Violation → advisory (exit 0, but reported) ──────────────────────────────

def test_unused_import_is_reported_non_blocking():
    code, out = _lint("import os\n")
    assert code == 0, f"Lint hook must never block, got {code}"
    assert "[lint-hook]" in out
    assert "F401" in out


def test_edit_tool_is_also_linted():
    # Edit is the other accepted write tool; same advisory behavior as Write.
    code, out = _lint("import sys\n", tool="Edit")
    assert code == 0
    assert "[lint-hook]" in out
    assert "F401" in out


# ── Silent exit 0 paths ──────────────────────────────────────────────────────

def test_clean_python_file_is_silent():
    code, out = _lint("x = 1\nprint(x)\n")
    assert code == 0
    assert out.strip() == "", f"Expected no output, got: {out!r}"


def test_non_py_file_is_ignored():
    code, out = _lint("import os\n", suffix=".txt")
    assert code == 0
    assert out.strip() == ""


def test_non_edit_write_tool_is_ignored():
    code, out = _lint("import os\n", tool="Bash")
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
