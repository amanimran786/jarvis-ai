#!/usr/bin/env python3
"""routine_scheduler.py — fire project_manager routines on their cron schedule.

Run as a long-lived daemon (e.g. via launchd or tmux). Wakes every minute,
checks all enabled routines, fires any that are due.

Usage:
  python3 scripts/routine_scheduler.py          # run until Ctrl-C
  python3 scripts/routine_scheduler.py --once   # check once and exit
  python3 scripts/routine_scheduler.py --dry-run  # print what would fire, don't fire
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [scheduler] %(message)s")
log = logging.getLogger("routine_scheduler")

# Shorthand schedules.
_SHORTHANDS = {
    "hourly":  "0 * * * *",
    "daily":   "0 9 * * *",
    "weekly":  "0 9 * * 1",
    "nightly": "0 0 * * *",
}


def _parse_cron(expr: str) -> tuple[set, set, set, set, set]:
    """Parse a 5-field cron expression into sets of allowed values."""
    expr = _SHORTHANDS.get(expr.lower().strip(), expr)
    fields = expr.strip().split()
    if len(fields) != 5:
        raise ValueError(f"Expected 5 cron fields, got {len(fields)!r} in {expr!r}")

    def _field(s: str, lo: int, hi: int) -> set[int]:
        if s == "*":
            return set(range(lo, hi + 1))
        out: set[int] = set()
        for part in s.split(","):
            if "/" in part:
                rng, step = part.split("/", 1)
                start = lo if rng == "*" else int(rng.split("-")[0])
                end = hi if rng == "*" else int(rng.split("-")[-1])
                out.update(range(start, end + 1, int(step)))
            elif "-" in part:
                a, b = part.split("-", 1)
                out.update(range(int(a), int(b) + 1))
            else:
                out.add(int(part))
        return out

    minute, hour, dom, month, dow = fields
    return (
        _field(minute, 0, 59),
        _field(hour, 0, 23),
        _field(dom, 1, 31),
        _field(month, 1, 12),
        _field(dow, 0, 6),
    )


def _is_due(schedule: str, dt: datetime) -> bool:
    try:
        minutes, hours, doms, months, dows = _parse_cron(schedule)
    except ValueError as exc:
        log.warning("bad schedule %r: %s", schedule, exc)
        return False
    return (
        dt.minute in minutes
        and dt.hour in hours
        and dt.day in doms
        and dt.month in months
        and dt.weekday() in {d % 7 for d in dows}  # cron 0=Sun, Python 0=Mon
    )


def _last_fired_minute(last_fired_at: str) -> str:
    """Return 'YYYY-MM-DD HH:MM' of last fire, for dedup."""
    if not last_fired_at:
        return ""
    return last_fired_at[:16]


def check_and_fire(*, dry_run: bool = False) -> list[str]:
    """Check all enabled routines and fire any that are due. Returns list of fired IDs."""
    import project_manager as pm
    pm._ensure_routines_schema()
    routines = pm.list_routines()
    now = datetime.now(timezone.utc)
    now_minute = now.strftime("%Y-%m-%dT%H:%M")
    fired: list[str] = []

    for r in routines:
        if not r.get("enabled", 1):
            continue
        schedule = r.get("schedule", "")
        if not schedule:
            continue
        if not _is_due(schedule, now):
            continue
        # Dedup: don't fire twice in the same minute.
        if _last_fired_minute(r.get("last_fired_at", "")) == now_minute[:16]:
            log.info("routine %s already fired this minute — skipping", r["id"])
            continue
        if dry_run:
            log.info("[dry-run] would fire routine %s %r", r["id"], r["name"])
            fired.append(r["id"])
            continue
        try:
            proj = pm.fire_routine(r["id"])
            log.info("fired routine %s %r → project %s", r["id"], r["name"], proj["id"])
            fired.append(r["id"])
        except Exception:
            log.exception("failed to fire routine %s", r["id"])

    return fired


def run(*, dry_run: bool = False) -> None:
    log.info("routine scheduler started (dry_run=%s)", dry_run)
    try:
        while True:
            check_and_fire(dry_run=dry_run)
            # Sleep until the top of the next minute.
            now = time.time()
            sleep_secs = 60 - (now % 60)
            time.sleep(max(1, sleep_secs))
    except KeyboardInterrupt:
        log.info("scheduler stopped")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="routine_scheduler")
    p.add_argument("--once", action="store_true", help="Check once and exit")
    p.add_argument("--dry-run", action="store_true", help="Print what would fire without firing")
    args = p.parse_args(argv)

    if args.once:
        fired = check_and_fire(dry_run=args.dry_run)
        print(f"[scheduler] fired {len(fired)} routine(s): {fired or 'none'}")
        return
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
