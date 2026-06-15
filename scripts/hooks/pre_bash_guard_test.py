#!/usr/bin/env python3
"""Tests for pre_bash_guard.py — validates block and warn patterns."""
import json
import subprocess
import sys
from pathlib import Path

_HOOK = Path(__file__).resolve().parent / "pre_bash_guard.py"


def _run(cmd: str) -> tuple[int, str]:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    r = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload, capture_output=True, text=True, timeout=5,
    )
    return r.returncode, r.stdout + r.stderr


def test_force_push_is_blocked():
    code, out = _run("git push origin main --force")
    assert code == 2, f"Expected 2, got {code}"
    assert "force" in out.lower()


def test_rm_rf_root_is_blocked():
    code, out = _run("rm -rf /")
    assert code == 2


def test_git_reset_hard_is_blocked():
    code, out = _run("git reset --hard HEAD~1")
    assert code == 2


def test_normal_git_status_is_allowed():
    code, _ = _run("git status")
    assert code == 0


def test_safe_pytest_is_allowed():
    code, _ = _run("python3 -m pytest tests/test_rbac.py -q")
    assert code == 0


def test_non_bash_tool_is_ignored():
    payload = json.dumps({"tool_name": "Edit", "tool_input": {"command": "rm -rf /"}})
    r = subprocess.run(
        [sys.executable, str(_HOOK)],
        input=payload, capture_output=True, text=True, timeout=5,
    )
    assert r.returncode == 0


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
