"""Pre-commit security and quality gate for Jarvis code sessions.

Run against every changed Python file before committing.  This module
automates the grep-based checks documented in REVIEW.md so code sessions
can call it programmatically instead of shelling out manually.

CLI usage
---------
    python -m harness.pre_commit_check path/to/file.py [more/files.py ...]

Exit codes
----------
    0   — all checks passed
    1   — one or more checks failed (details printed to stderr)
    2   — usage error / no files given

API usage
---------
    from harness.pre_commit_check import run_checks, CheckResult
    result: CheckResult = run_checks(["path/to/file.py"])
    if not result.passed:
        for finding in result.findings:
            print(finding)
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class Finding:
    """One security or quality violation found in a file."""

    file: str
    line: int
    rule: str
    text: str

    def __str__(self) -> str:
        return f"{self.file}:{self.line}: [{self.rule}] {self.text.strip()}"


@dataclass
class CheckResult:
    """Aggregate result of running all checks against a set of files."""

    passed: bool
    files_checked: list[str] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    syntax_errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.passed:
            return (
                f"REVIEW gate PASSED — {len(self.files_checked)} file(s) clean"
            )
        parts = []
        if self.findings:
            parts.append(
                f"{len(self.findings)} security finding(s)"
            )
        if self.syntax_errors:
            parts.append(
                f"{len(self.syntax_errors)} syntax error(s)"
            )
        files = ", ".join(self.files_checked) or "(none)"
        return f"REVIEW gate FAILED — {'; '.join(parts)} in: {files}"


# ---------------------------------------------------------------------------
# Rule definitions
# ---------------------------------------------------------------------------


# Each rule: (rule_id, regex_pattern, description)
# A match on any line means the file fails that rule.
_RULES: list[tuple[str, str, str]] = [
    (
        "SHELL_TRUE",
        r"shell\s*=\s*True",
        "subprocess call with shell=True (RCE risk) — use list args + shell=False",  # pre-commit-ok
    ),
    (
        "EVAL_EXEC",
        r"\beval\s*\(|\bexec\s*\(",
        "eval() or exec() usage — never pass STT/LLM output here",  # pre-commit-ok
    ),
    (
        "HARDCODED_SECRET",
        r"\b(SECRET|API_KEY|TOKEN|PASSWORD)\w*\s*=\s*[\"'][^\"']+[\"']",
        "hardcoded secret literal — use os.getenv() or config.",
    ),
    (
        "UNSAFE_DESERIALIZE",
        r"\bpickle\.load\s*\(|\byaml\.load\s*\(",
        "unsafe deserialization — use pickle.loads with trusted sources only, yaml.safe_load",
    ),
]

# Pattern for lines that legitimately reference secret-like names without
# hardcoding values (env lookups, config references).  These are excluded
# from HARDCODED_SECRET matching.
_SECRET_ALLOWLIST = re.compile(r"os\.getenv|config\.", re.IGNORECASE)


def _check_file_rules(path: Path) -> list[Finding]:
    """Return security findings for one file."""
    findings: list[Finding] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        findings.append(
            Finding(
                file=str(path),
                line=0,
                rule="IO_ERROR",
                text=f"could not read file: {exc}",
            )
        )
        return findings

    for rule_id, pattern, description in _RULES:
        regex = re.compile(pattern)
        for lineno, line in enumerate(lines, start=1):
            if not regex.search(line):
                continue
            # Inline suppression: # pre-commit-ok skips any rule on that line
            if "# pre-commit-ok" in line:
                continue
            # Allow-list: skip HARDCODED_SECRET lines that use safe lookups
            if rule_id == "HARDCODED_SECRET" and _SECRET_ALLOWLIST.search(line):
                continue
            findings.append(
                Finding(
                    file=str(path),
                    line=lineno,
                    rule=rule_id,
                    text=f"{description}\n    {line.strip()}",
                )
            )
    return findings


def _syntax_check(path: Path) -> str | None:
    """Return an error string if *path* fails py_compile, else None."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "py_compile", str(path)],
        capture_output=True,
        text=True,
        shell=False,
        timeout=30,
    )
    if result.returncode != 0:
        msg = (result.stderr or result.stdout).strip()
        return f"{path}: {msg}"
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_checks(files: Sequence[str | Path]) -> CheckResult:
    """Run all REVIEW.md automated checks on *files*.

    Args:
        files: Paths to Python source files to check.

    Returns:
        A :class:`CheckResult` with ``passed=True`` if every check is clean.
    """
    paths = [Path(f) for f in files]
    all_findings: list[Finding] = []
    syntax_errors: list[str] = []
    checked: list[str] = []

    for path in paths:
        if not path.exists():
            all_findings.append(
                Finding(
                    file=str(path),
                    line=0,
                    rule="FILE_NOT_FOUND",
                    text=f"file does not exist: {path}",
                )
            )
            continue
        checked.append(str(path))
        all_findings.extend(_check_file_rules(path))
        err = _syntax_check(path)
        if err is not None:
            syntax_errors.append(err)

    passed = not all_findings and not syntax_errors
    return CheckResult(
        passed=passed,
        files_checked=checked,
        findings=all_findings,
        syntax_errors=syntax_errors,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = (argv if argv is not None else sys.argv)[1:]
    if not args:
        print(
            "Usage: python -m harness.pre_commit_check <file.py> [...]",
            file=sys.stderr,
        )
        return 2

    result = run_checks(args)

    if result.findings:
        print("\n=== Security Findings ===", file=sys.stderr)
        for finding in result.findings:
            print(f"  {finding}", file=sys.stderr)

    if result.syntax_errors:
        print("\n=== Syntax Errors ===", file=sys.stderr)
        for err in result.syntax_errors:
            print(f"  {err}", file=sys.stderr)

    print(f"\n{result.summary()}", file=sys.stderr if not result.passed else sys.stdout)
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
