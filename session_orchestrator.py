#!/usr/bin/env python3
"""
File-based session orchestrator for Jarvis AI dev sessions.

Coordinates active Claude/Codex dev lanes through shared JSON files.
Runs a live terminal dashboard that refreshes every 30 seconds.

Usage:
    python session_orchestrator.py              # live dashboard
    python session_orchestrator.py status       # one-shot status and exit
    python session_orchestrator.py add-task "session" "task" priority
    python session_orchestrator.py history      # tail MASTER_LOG.md
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ── paths ────────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parent
ORCHESTRATOR_STATUS = ROOT / "ORCHESTRATOR_STATUS.json"
WORK_QUEUE = ROOT / "WORK_QUEUE.json"
SESSIONS_FILE = ROOT / "SESSIONS.json"
MASTER_LOG = ROOT / "MASTER_LOG.md"
LOGS_DIR = ROOT / "logs"
ORCH_LOG = LOGS_DIR / "orchestrator.log"

STALL_THRESHOLD_SECONDS = 300   # 5 minutes
REFRESH_INTERVAL = 30           # dashboard refresh cadence

# ── logging ──────────────────────────────────────────────────────────────────

LOGS_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(ORCH_LOG),
    ],
)
log = logging.getLogger("session_orchestrator")

# ── rich (optional) ──────────────────────────────────────────────────────────

try:
    from rich.console import Console
    from rich.table import Table
    from rich.live import Live
    from rich.panel import Panel
    from rich.text import Text
    from rich.columns import Columns
    from rich import box
    import rich.padding
    RICH = True
    console = Console()
except ImportError:
    RICH = False
    console = None  # type: ignore[assignment]

# ── ANSI fallback ─────────────────────────────────────────────────────────────

_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "cyan": "\033[36m",
    "blue": "\033[34m",
    "dim": "\033[2m",
}


def _c(color: str, text: str) -> str:
    return f"{_ANSI.get(color, '')}{text}{_ANSI['reset']}"


# ── file I/O helpers ─────────────────────────────────────────────────────────

def _read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return default
        return json.loads(raw)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to read %s: %s", path.name, exc)
        return default


def _write_json(path: Path, data: Any) -> None:
    """Atomic write via temp file to avoid partial reads."""
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.error("Failed to write %s: %s", path.name, exc)


def _append_master_log(session: str, event: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] [{session}] {event}\n"
    try:
        with MASTER_LOG.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError as exc:
        log.error("Failed to append MASTER_LOG.md: %s", exc)


# ── status parsing ────────────────────────────────────────────────────────────

def _parse_age_seconds(iso_ts: str | None) -> float:
    """Return seconds since iso_ts, or inf if unparseable."""
    if not iso_ts:
        return float("inf")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(iso_ts, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - dt).total_seconds()
        except ValueError:
            continue
    return float("inf")


def _session_health(session: dict) -> str:
    """Derive session health from last_active timestamp + reported status.

    Stall detection only fires on sessions that claim to be 'active' — a
    session that says 'idle' or 'offline' is trusted at its word regardless
    of how long ago it last updated. This prevents seed entries and cleanly-
    shut-down sessions from showing as STALLED.
    """
    reported = session.get("status", "unknown")
    # Trust explicit terminal / rest states immediately.
    if reported in ("offline", "error", "stalled"):
        return reported
    if reported in ("idle", "done", "complete", "waiting"):
        return "idle"
    # For 'active' (or unknown) sessions, apply the stall threshold.
    age = _parse_age_seconds(session.get("last_active"))
    if age == float("inf"):
        return "unknown"
    if age > STALL_THRESHOLD_SECONDS:
        return "stalled"
    return "active"


def _fmt_age(seconds: float) -> str:
    if seconds == float("inf"):
        return "never"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h {m}m ago"
    if m:
        return f"{m}m {s}s ago"
    return f"{s}s ago"


def _health_text_rich(health: str) -> "Text":
    colors = {
        "active": "green",
        "idle": "cyan",
        "stalled": "bold red",
        "unknown": "dim",
        "offline": "yellow",
        "error": "bold red",
    }
    return Text(health.upper(), style=colors.get(health, "white"))


def _health_text_ansi(health: str) -> str:
    colors = {
        "active": "green",
        "idle": "cyan",
        "stalled": "red",
        "unknown": "dim",
        "offline": "yellow",
    }
    return _c(colors.get(health, "reset"), health.upper())


# ── data loaders ──────────────────────────────────────────────────────────────

def load_session_statuses() -> list[dict]:
    """Load sessions from ORCHESTRATOR_STATUS.json.

    harness/audit.py writes sessions as a list of dicts under the key
    "sessions". Handles both list-of-dicts and dict-of-dicts gracefully.
    """
    data = _read_json(ORCHESTRATOR_STATUS, {})
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    raw = data.get("sessions", [])
    # harness/audit.py writes a list; if someone wrote a dict keyed by name,
    # convert it to list form so the display code is uniform.
    if isinstance(raw, dict):
        return [{"name": k, **v} for k, v in raw.items()]
    if isinstance(raw, list):
        return raw
    return []


def load_queue() -> list[dict]:
    data = _read_json(WORK_QUEUE, [])
    return data if isinstance(data, list) else []


def load_registered_sessions() -> list[dict]:
    data = _read_json(SESSIONS_FILE, {})
    if isinstance(data, dict):
        return data.get("sessions", [])
    return []


# ── stall detection ───────────────────────────────────────────────────────────

_last_stall_logged: set[str] = set()


def check_stalls(sessions: list[dict]) -> list[str]:
    """Return names of stalled sessions. Log + master-log new stalls only."""
    stalled = []
    for s in sessions:
        name = s.get("name", "unknown")
        if _session_health(s) == "stalled":
            stalled.append(name)
            if name not in _last_stall_logged:
                age = _parse_age_seconds(s.get("last_active"))
                msg = f"STALL detected — last active {_fmt_age(age)}, current_task={s.get('current_task')!r}"
                log.warning("[%s] %s", name, msg)
                _append_master_log(name, msg)
                _last_stall_logged.add(name)
        else:
            _last_stall_logged.discard(name)
    return stalled


# ── rich dashboard rendering ──────────────────────────────────────────────────

def _build_rich_panel(
    sessions: list[dict],
    queue: list[dict],
    stalled: list[str],
) -> "Panel":
    # Sessions table
    st = Table(box=box.ROUNDED, expand=True, show_lines=False, title="Sessions")
    st.add_column("Session", no_wrap=True, min_width=28)
    st.add_column("Health", justify="center", width=10)
    st.add_column("Last Seen", width=14)
    st.add_column("Current Task")
    st.add_column("Next Task", style="dim")

    if sessions:
        for s in sessions:
            name = s.get("name", "?")
            health = _session_health(s)
            age = _parse_age_seconds(s.get("last_active"))
            flag = " ⚠" if name in stalled else ""
            st.add_row(
                name + flag,
                _health_text_rich(health),
                _fmt_age(age),
                s.get("current_task") or "—",
                s.get("next_task") or "—",
            )
    else:
        st.add_row(
            "[dim]No sessions in ORCHESTRATOR_STATUS.json yet[/dim]",
            "", "", "", "",
        )

    # Queue table
    qt = Table(box=box.ROUNDED, expand=True, show_lines=False, title="Work Queue")
    qt.add_column("P", justify="right", width=3)
    qt.add_column("Session", width=26, no_wrap=True)
    qt.add_column("Status", width=12, justify="center")
    qt.add_column("Task")

    status_style = {
        "queued": "yellow",
        "in_progress": "green",
        "done": "dim",
        "cancelled": "red",
        "blocked": "yellow",
    }

    active_queue = [i for i in queue if i.get("status") != "done"]
    done_queue = [i for i in queue if i.get("status") == "done"]
    display_queue = sorted(active_queue, key=lambda x: x.get("priority", 99)) + done_queue[-5:]

    if display_queue:
        for item in display_queue:
            status = item.get("status", "queued")
            sty = status_style.get(status, "")
            status_cell = Text(status, style=sty) if sty else Text(status)
            qt.add_row(
                str(item.get("priority", "?")),
                item.get("session_name", "?"),
                status_cell,
                item.get("task", "?"),
            )
    else:
        qt.add_row("—", "—", Text("—"), "[dim]Queue empty[/dim]")

    ts = datetime.now().strftime("%H:%M:%S")
    content = rich.padding.Padding(
        Columns([st, qt], expand=True, equal=True),
        (1, 0, 0, 0),
    )
    return Panel(
        content,
        title=f"[bold cyan]Jarvis Session Orchestrator[/bold cyan]  [dim]updated {ts}[/dim]",
        subtitle="[dim]Ctrl-C to exit  ·  python orchestrator.py add-task ...[/dim]",
        border_style="blue",
    )


# ── plain-text dashboard ───────────────────────────────────────────────────────

def _print_plain_dashboard(
    sessions: list[dict],
    queue: list[dict],
    stalled: list[str],
) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(_c("bold", f"\n{'='*72}"))
    print(_c("cyan", f"  Jarvis Session Orchestrator  [{ts}]"))
    print(_c("bold", f"{'='*72}"))

    print(_c("bold", "\nSESSIONS"))
    if not sessions:
        print("  (no sessions in ORCHESTRATOR_STATUS.json yet)")
    for s in sessions:
        name = s.get("name", "?")
        health = _session_health(s)
        age = _parse_age_seconds(s.get("last_active"))
        flag = _c("red", "  ⚠ STALL") if name in stalled else ""
        print(f"  {_health_text_ansi(health):<12}  {name}{flag}")
        print(f"    last seen  : {_fmt_age(age)}")
        if s.get("current_task"):
            print(f"    current    : {s['current_task']}")
        if s.get("next_task"):
            print(f"    next       : {s['next_task']}")

    active_q = [i for i in queue if i.get("status") != "done"]
    done_q = [i for i in queue if i.get("status") == "done"]
    print(_c("bold", "\nWORK QUEUE"))
    if not active_q and not done_q:
        print("  (empty)")
    for item in sorted(active_q, key=lambda x: x.get("priority", 99)):
        status = item.get("status", "queued")
        color = {"queued": "yellow", "in_progress": "green", "blocked": "yellow"}.get(status, "reset")
        print(
            f"  [{item.get('priority','?')}] {_c(color, f'{status:<11}')}"
            f"  {item.get('session_name','?'):<26}  {item.get('task','?')}"
        )
    if done_q:
        print(_c("dim", f"  ... and {len(done_q)} completed task(s)"))

    print()


# ── commands ──────────────────────────────────────────────────────────────────

def cmd_status() -> None:
    sessions = load_session_statuses()
    queue = load_queue()
    stalled = check_stalls(sessions)
    if RICH:
        console.print(_build_rich_panel(sessions, queue, stalled))
    else:
        _print_plain_dashboard(sessions, queue, stalled)


def cmd_dashboard() -> None:
    """Live auto-refreshing dashboard."""
    _append_master_log("orchestrator", "Dashboard started")
    if RICH:
        try:
            with Live(console=console, refresh_per_second=0.03, screen=False) as live:
                while True:
                    sessions = load_session_statuses()
                    queue = load_queue()
                    stalled = check_stalls(sessions)
                    live.update(_build_rich_panel(sessions, queue, stalled))
                    time.sleep(REFRESH_INTERVAL)
        except KeyboardInterrupt:
            console.print("\n[dim]Orchestrator stopped.[/dim]")
    else:
        try:
            while True:
                os.system("clear")
                sessions = load_session_statuses()
                queue = load_queue()
                stalled = check_stalls(sessions)
                _print_plain_dashboard(sessions, queue, stalled)
                print(f"  [auto-refresh every {REFRESH_INTERVAL}s — Ctrl-C to stop]")
                time.sleep(REFRESH_INTERVAL)
        except KeyboardInterrupt:
            print("\nOrchestrator stopped.")


def cmd_add_task(session_name: str, task: str, priority: int) -> None:
    queue = load_queue()
    entry: dict = {
        "session_name": session_name,
        "task": task,
        "priority": priority,
        "status": "queued",
        "assigned_at": None,
        "completed_at": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    queue.append(entry)
    queue.sort(key=lambda x: (x.get("priority", 99), x.get("created_at", "")))
    _write_json(WORK_QUEUE, queue)
    _append_master_log(session_name, f"Task added (p{priority}): {task}")
    log.info("Task added — session=%s priority=%s task=%s", session_name, priority, task)
    if RICH:
        console.print(f"[green]✓[/green] Added task to [{session_name}] (priority {priority}): {task}")
    else:
        print(f"Added: [{session_name}] p{priority} — {task}")


def cmd_history() -> None:
    if not MASTER_LOG.exists():
        print("MASTER_LOG.md does not exist yet (no events logged).")
        return
    text = MASTER_LOG.read_text(encoding="utf-8")
    if RICH:
        from rich.syntax import Syntax
        console.print(Syntax(text, "markdown", theme="monokai", line_numbers=False))
    else:
        print(text)


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    args = sys.argv[1:]

    if not args:
        cmd_dashboard()
        return

    cmd = args[0]

    if cmd == "status":
        cmd_status()

    elif cmd == "add-task":
        if len(args) < 4:
            print("Usage: python session_orchestrator.py add-task <session> <task> <priority>")
            sys.exit(1)
        try:
            priority = int(args[3])
        except ValueError:
            print(f"Priority must be an integer, got: {args[3]!r}")
            sys.exit(1)
        cmd_add_task(args[1], args[2], priority)

    elif cmd == "history":
        cmd_history()

    else:
        print(f"Unknown command: {cmd!r}")
        print("Commands: [no args] | status | add-task <session> <task> <priority> | history")
        sys.exit(1)


if __name__ == "__main__":
    main()
