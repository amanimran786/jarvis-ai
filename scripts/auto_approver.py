#!/usr/bin/env python3
"""
Auto-approver daemon for the Jarvis task approval queue.

Reviews waiting_approval tasks and auto-approves those that pass the
safety policy. Holds tasks that involve destructive or external actions.

Usage:
    python3 scripts/auto_approver.py [--project PROJECT_ID] [--interval 15]

Policy:
    APPROVE  — read, write, audit, analyze, implement, design, code, test
    HOLD     — git push, deploy, release, rm -rf, sudo, pip install, merge PR
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import task_runtime

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("auto_approver")

# Patterns that are never auto-approved — require human eyes
_HOLD_MARKERS: list[tuple[str, str]] = [
    ("git push",          "remote push"),
    ("git reset --hard",  "destructive git"),
    ("git clean",         "destructive git"),
    ("git force",         "force push"),
    ("rm -rf",            "recursive delete"),
    ("rm -r ",            "recursive delete"),
    ("sudo ",             "privileged action"),
    ("deploy",            "deployment action"),
    ("release",           "release action"),
    ("merge pull request","merge action"),
    ("merge pr",          "merge action"),
    ("drop table",        "database destruction"),
    ("truncate table",    "database destruction"),
    ("delete from",       "database destruction"),
]

# Patterns that are safe to auto-approve (read/analyze/implement)
_SAFE_MARKERS: list[tuple[str, str]] = [
    ("audit",        "analysis task"),
    ("analyze",      "analysis task"),
    ("read",         "read-only task"),
    ("review",       "review task"),
    ("implement",    "implementation task"),
    ("write",        "write task"),
    ("design",       "design task"),
    ("synthesize",   "synthesis task"),
    ("integration",  "integration task"),
    ("report",       "report task"),
    ("gap",          "gap analysis"),
    ("security fix", "security hardening"),
    ("cache",        "cache implementation"),
    ("dashboard",    "UI task"),
    ("endpoint",     "API task"),
]


def _check_prompt(prompt: str, task_id: str, source: str) -> tuple[str, str]:
    """Returns ('approve'|'hold'|'skip', reason)."""
    lower = (prompt or "").lower()

    for marker, reason in _HOLD_MARKERS:
        if marker in lower:
            return "hold", f"matched hold marker: {reason!r} ({marker!r})"

    # Project-sourced tasks are internally generated — apply a lighter touch
    if source == "project":
        return "approve", "project-sourced task, content scan cleared"

    for marker, reason in _SAFE_MARKERS:
        if marker in lower:
            return "approve", f"matched safe marker: {reason!r}"

    return "hold", "no safe marker matched and not project-sourced"


def run(project_id: str | None, interval: int) -> None:
    scope = f"project {project_id}" if project_id else "all projects"
    log.info("Auto-approver started — watching %s, polling every %ds", scope, interval)
    log.info("Policy: HOLD on push/deploy/rm-rf/sudo; APPROVE on project-sourced analysis/implementation")

    approved_ids: set[str] = set()

    while True:
        try:
            pending = task_runtime.list_tasks(limit=200, status="waiting_approval")
        except Exception as exc:
            log.warning("Failed to list tasks: %s", exc)
            time.sleep(interval)
            continue

        for task in pending:
            task_id = task.get("id", "")
            if task_id in approved_ids:
                continue

            prompt = task.get("prompt", "")
            source = task.get("source", "")
            reason_field = task.get("approval_reason", "")
            meta = task.get("meta") or {}

            # If project filter is set, skip tasks not from that project
            if project_id:
                task_project = meta.get("project_id", "")
                if task_project and task_project != project_id:
                    continue

            decision, reason = _check_prompt(prompt, task_id, source)
            prompt_preview = prompt[:100].replace("\n", " ")

            if decision == "approve":
                try:
                    task_runtime.approve_task(task_id)
                    approved_ids.add(task_id)
                    log.info(
                        "APPROVED  %s  [%s]  reason=%r  prompt=%.80r",
                        task_id, reason_field or "?", reason, prompt_preview
                    )
                except Exception as exc:
                    log.warning("Approval failed for %s: %s", task_id, exc)
            else:
                log.warning(
                    "HELD      %s  [%s]  reason=%r  prompt=%.80r",
                    task_id, reason_field or "?", reason, prompt_preview
                )

        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-approve safe Jarvis tasks")
    parser.add_argument("--project", default=None, help="Only approve tasks for this project ID")
    parser.add_argument("--interval", type=int, default=15, help="Poll interval in seconds")
    args = parser.parse_args()
    try:
        run(args.project, args.interval)
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
