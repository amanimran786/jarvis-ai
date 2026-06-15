"""monitor.py — unified live monitor for all agent activity.

Shows orchestrate ADE lanes AND project_manager projects in one view.
Refreshes every 3 seconds. Press Ctrl-C to exit.

Usage:
  python3 monitor.py                    # full dashboard (projects + lanes)
  python3 monitor.py --projects-only    # only project_manager projects
  python3 monitor.py --lanes-only       # only ADE worktree lanes
  python3 monitor.py tail <project_id>  # live event tail for one project
  python3 monitor.py once               # print once and exit (good for CI)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_REPO_ROOT))

_REFRESH = float(os.getenv("JARVIS_MONITOR_REFRESH", "3"))
_CLEAR = os.getenv("JARVIS_MONITOR_NO_CLEAR", "") not in ("1", "true")


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def _clear() -> None:
    if _CLEAR:
        os.system("clear")  # noqa: S605,S607


# ── Data collection ───────────────────────────────────────────────────────────

def _collect_projects() -> list[dict]:
    try:
        import project_manager as pm
        return pm.collect_status()
    except Exception as exc:
        return [{"_error": str(exc)}]


def _collect_lanes() -> list[dict]:
    try:
        from orchestrate import collect_status
        return collect_status()
    except Exception as exc:
        return [{"_error": str(exc)}]


def _collect_tasks() -> dict:
    try:
        import task_runtime
        task_runtime.bootstrap()
        tasks = task_runtime.list_tasks(limit=50)
        agents = task_runtime.list_agents()
        active = [t for t in tasks if t.get("status") in
                  ("queued", "assigned", "running", "streaming", "waiting_approval")]
        busy_agents = [a for a in agents if a.get("status") == "busy"]
        return {
            "active": active,
            "busy_agents": busy_agents,
            "total": len(tasks),
        }
    except Exception as exc:
        return {"_error": str(exc), "active": [], "busy_agents": [], "total": 0}


# ── Renderers ─────────────────────────────────────────────────────────────────

_STATUS_ICONS = {
    "pending":   "⏸",
    "running":   "▶",
    "done":      "✓",
    "DONE":      "✓",
    "failed":    "✗",
    "FAILED":    "✗",
    "cancelled": "✕",
    "RUNNING":   "▶",
    "queued":    "⏳",
    "assigned":  "⏳",
    "succeeded": "✓",
    "waiting_approval": "⏸",
}


def _icon(status: str) -> str:
    return _STATUS_ICONS.get(status, "?")


def _render_projects(rows: list[dict]) -> str:
    if not rows:
        return "  (no projects)"
    if rows and "_error" in rows[0]:
        return f"  [error] {rows[0]['_error']}"
    lines = []
    hdr = f"  {'ID':<18} {'TITLE':<38} {'AGENT':<20} {'ST':<10} {'↻':<2} {'PROG':<10} {'UPDATED'}"
    lines.append(hdr)
    lines.append("  " + "─" * 108)
    for r in rows:
        ic = _icon(r["status"])
        live = "●" if r.get("running") else "○"
        prog = f"{r.get('tasks_done',0)}/{r.get('tasks_total',0)}"
        if r.get("tasks_failed"):
            prog += f"({r['tasks_failed']}✗)"
        lines.append(
            f"  {r['id']:<18} {r['title']:<38} {r['agent']:<20} "
            f"{ic} {r['status']:<8} {live:<2} {prog:<10} {r['updated_at'][11:19]}"
        )
    return "\n".join(lines)


def _render_lanes(rows: list[dict]) -> str:
    if not rows:
        return "  (no ADE lanes)"
    if rows and "_error" in rows[0]:
        return f"  [error] {rows[0]['_error']}"
    lines = []
    hdr = f"  {'LANE':<22} {'ST':<12} {'↻':<2} {'RET':<4} {'DIFF':<12} {'SYNC':<5} PROMPT"
    lines.append(hdr)
    lines.append("  " + "─" * 80)
    for r in rows:
        ic = _icon(r.get("status", "?"))
        alive = "●" if r.get("alive") else "○"
        diff = f"{r.get('files',0)}f/{r.get('lines',0)}L" if r.get("files") else "—"
        sync = "▲" if r.get("ready_to_sync") else ""
        lines.append(
            f"  {r['lane']:<22} {ic} {r.get('status','?'):<10} {alive:<2} "
            f"{r.get('retries',0):<4} {diff:<12} {sync:<5} {r.get('prompt','')[:40]}"
        )
    return "\n".join(lines)


def _render_tasks(data: dict) -> str:
    if "_error" in data:
        return f"  [error] {data['_error']}"
    active = data.get("active", [])
    busy = data.get("busy_agents", [])
    if not active and not busy:
        return "  (no active tasks)"
    lines = []
    for t in active[:8]:
        agent = t.get("assigned_agent_id", "?")
        ic = _icon(t.get("status", ""))
        prompt = (t.get("prompt") or "")[:60]
        lines.append(f"  {ic} [{agent}]  {t['id'][:16]}  {prompt}")
    if len(active) > 8:
        lines.append(f"  … and {len(active)-8} more")
    return "\n".join(lines) if lines else "  (no active tasks)"


def _render_summary(projects: list[dict], lanes: list[dict], tasks: dict) -> str:
    active_projects = sum(1 for p in projects if p.get("status") == "running")
    done_projects = sum(1 for p in projects if p.get("status") == "done")
    active_lanes = sum(1 for l in lanes if l.get("alive"))
    sync_ready = sum(1 for l in lanes if l.get("ready_to_sync"))
    active_tasks = len(tasks.get("active", []))

    parts = []
    if active_projects:
        parts.append(f"projects running: {active_projects}")
    if done_projects:
        parts.append(f"done: {done_projects}")
    if active_lanes:
        parts.append(f"ADE lanes: {active_lanes}")
    if sync_ready:
        parts.append(f"ready to harvest: {sync_ready}")
    if active_tasks:
        parts.append(f"active tasks: {active_tasks}")
    return "  " + ("  ·  ".join(parts) if parts else "all idle")


# ── Full dashboard render ─────────────────────────────────────────────────────

def render_dashboard(
    projects: list[dict],
    lanes: list[dict],
    tasks: dict,
    *,
    show_projects: bool = True,
    show_lanes: bool = True,
    show_tasks: bool = True,
) -> str:
    ts = _now_str()
    try:
        width = os.get_terminal_size().columns
    except OSError:
        width = 100
    title = f" JARVIS AGENT MONITOR  {ts} "
    pad = max(0, width - len(title))
    lines = [
        "═" * width,
        title + " " * pad,
        "═" * width,
    ]

    if show_projects:
        lines.append(f"\n── PROJECTS ({len(projects)}) " + "─" * max(0, width - 15 - len(str(len(projects)))))
        lines.append(_render_projects(projects))

    if show_lanes:
        lines.append(f"\n── ADE LANES ({len(lanes)}) " + "─" * max(0, width - 15 - len(str(len(lanes)))))
        lines.append(_render_lanes(lanes))

    if show_tasks:
        n = len(tasks.get("active", []))
        lines.append(f"\n── ACTIVE TASKS ({n}) " + "─" * max(0, width - 17 - len(str(n))))
        lines.append(_render_tasks(tasks))

    lines.append("\n── SUMMARY " + "─" * max(0, width - 11))
    lines.append(_render_summary(projects, lanes, tasks))
    lines.append("\n  [Ctrl-C to exit]  [refresh every 3s]")
    return "\n".join(lines)


# ── Live tail for a single project ────────────────────────────────────────────

def _live_tail(project_id: str) -> None:
    import project_manager as pm
    proj = pm.get_project(project_id)
    if proj is None:
        print(f"[monitor] Project not found: {project_id}")
        sys.exit(1)

    print(f"[monitor] {project_id}  {proj['title']!r}  agent={proj['agent_id']}  status={proj['status']}")
    print("─" * 80)
    since_id = 0
    try:
        while True:
            events = pm.tail_events(project_id, since_id=since_id)
            for ev in events:
                payload = json.loads(ev.get("payload_json") or "{}")
                ts = (ev.get("ts") or "")[:19]
                seq = f"[t{ev['task_seq']}]" if ev.get("task_seq") is not None else "    "
                et = ev["event_type"]
                if et == "task_started":
                    print(f"  {ts} {seq} ▶  {payload.get('title', '')}")
                elif et == "task_done":
                    excerpt = payload.get("result_excerpt", "")[:120]
                    print(f"  {ts} {seq} ✓  {payload.get('title', '')}  →  {excerpt}")
                elif et in ("task_failed", "task_timeout"):
                    print(f"  {ts} {seq} ✗  {et}  {payload.get('error', '')}")
                elif et == "project_done":
                    print(f"  {ts}       ★  PROJECT DONE")
                elif et == "project_error":
                    print(f"  {ts}       ✗  ERROR: {payload.get('error', '')}")
                elif et == "heartbeat":
                    print(f"  {ts} {seq} ♡  heartbeat")
                elif et == "status_change":
                    print(f"  {ts}       →  {payload.get('status', '')}")
                else:
                    print(f"  {ts} {seq} {et}  {json.dumps(payload)[:80]}")
                since_id = max(since_id, ev["id"])

            proj_now = pm.get_project(project_id)
            if proj_now and proj_now["status"] in ("done", "failed", "cancelled") and not events:
                print(f"\n[monitor] Project is {proj_now['status']}. Exiting.")
                break
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n[monitor] Detached.")


# ── Main loop ─────────────────────────────────────────────────────────────────

def run_dashboard(
    *,
    show_projects: bool = True,
    show_lanes: bool = True,
    show_tasks: bool = True,
    once: bool = False,
) -> None:
    try:
        while True:
            projects = _collect_projects() if show_projects else []
            lanes = _collect_lanes() if show_lanes else []
            tasks = _collect_tasks() if show_tasks else {"active": [], "busy_agents": [], "total": 0}

            if not once:
                _clear()
            print(render_dashboard(
                projects, lanes, tasks,
                show_projects=show_projects,
                show_lanes=show_lanes,
                show_tasks=show_tasks,
            ))
            if once:
                break
            time.sleep(_REFRESH)
    except KeyboardInterrupt:
        print("\n[monitor] Exited.")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="monitor", description="Live monitor for Jarvis agent activity.")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("once", help="Print dashboard once and exit")

    t = sub.add_parser("tail", help="Live event tail for a project")
    t.add_argument("project_id")

    p.add_argument("--projects-only", action="store_true")
    p.add_argument("--lanes-only", action="store_true")
    p.add_argument("--no-tasks", action="store_true")

    args = p.parse_args(argv)

    if args.command == "tail":
        _live_tail(args.project_id)
        return
    if args.command == "once":
        run_dashboard(once=True)
        return

    run_dashboard(
        show_projects=not getattr(args, "lanes_only", False),
        show_lanes=not getattr(args, "projects_only", False),
        show_tasks=not getattr(args, "no_tasks", False),
    )


if __name__ == "__main__":
    main()
