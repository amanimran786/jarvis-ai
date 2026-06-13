#!/usr/bin/env python3
"""Run Jarvis's local-first upgrade loop.

Examples:
  python scripts/upgrade_loop.py --brief-file research.md --json
  python scripts/upgrade_loop.py --watch --brief-file research.md --interval 1800
  python scripts/upgrade_loop.py --summary --json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core import upgrade_loop


def _read_inputs(args: argparse.Namespace) -> list[str]:
    briefs: list[str] = []
    if args.brief:
        briefs.append(args.brief)
    for item in args.brief_file or []:
        path = Path(item).expanduser()
        briefs.append(path.read_text(encoding="utf-8"))
    if args.watchlist:
        briefs.append(json.dumps({"signals": upgrade_loop.DEFAULT_WATCHLIST}))
    return briefs


def _cycle_inputs(args: argparse.Namespace) -> tuple[list[str], dict]:
    """Read cycle inputs fresh so --watch can detect new source changes."""
    briefs = _read_inputs(args)
    fetch_report = {}
    if args.fetch_watchlist:
        signals, fetch_report = upgrade_loop.fetch_watchlist_signals(force=args.force_fetch_signal)
        if signals:
            briefs.append(json.dumps({"signals": [s.__dict__ for s in signals]}))
    return briefs, fetch_report


def _print(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    if payload.get("type") == "summary":
        print(f"cycles={payload['summary']['cycle_count']} candidates={payload['summary']['candidate_count']}")
        print(f"decisions={payload['summary']['decisions']}")
        return
    metrics = payload.get("metrics", {})
    print(f"cycle={payload.get('cycle_id')} signals={payload.get('signal_count')} candidates={metrics.get('candidate_count', 0)}")
    for cand in payload.get("candidates", [])[:10]:
        review = " review" if cand.get("requires_review") else ""
        print(f"- P{cand.get('priority')} score={cand.get('score')} {cand.get('category')}{review}: {cand.get('title')}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Jarvis local-first upgrade loop")
    parser.add_argument("--brief", action="append", help="Inline upgrade brief or finding")
    parser.add_argument("--brief-file", action="append", help="Path to a research/finding brief")
    parser.add_argument("--source", default="manual", help="Source label for the cycle")
    parser.add_argument("--limit", type=int, default=10, help="Maximum candidates to keep")
    parser.add_argument("--watchlist", action="store_true", help="Include built-in upgrade watchlist seeds")
    parser.add_argument("--fetch-watchlist", action="store_true",
                        help="Fetch built-in public watchlist sources and create signals on content changes")
    parser.add_argument("--force-fetch-signal", action="store_true",
                        help="Treat fetched watchlist sources as changed even if digest is unchanged")
    parser.add_argument("--create-tickets", action="store_true", help="Append durable local upgrade tickets")
    parser.add_argument("--auto-promote", action="store_true",
                        help="Promote low-risk high-scoring tickets to local backlog only")
    parser.add_argument("--sandbox-smoke", action="store_true",
                        help="Run local upgrade-loop safety smoke tests after candidate generation")
    parser.add_argument("--summary", action="store_true", help="Print ledger summary instead of running a cycle")
    parser.add_argument("--decision", choices=["accepted", "rejected", "deferred", "needs_more_research"], help="Record a human decision")
    parser.add_argument("--candidate-id", help="Candidate id for --decision")
    parser.add_argument("--reason", default="", help="Decision reason")
    parser.add_argument("--json", action="store_true", help="Print JSON output")
    parser.add_argument("--watch", action="store_true", help="Repeat at --interval seconds")
    parser.add_argument("--interval", type=int, default=1800, help="Watch interval in seconds")
    args = parser.parse_args(argv)

    if args.summary:
        _print({"type": "summary", "summary": upgrade_loop.summarize()}, args.json)
        return 0
    if args.decision:
        if not args.candidate_id:
            parser.error("--decision requires --candidate-id")
        payload = upgrade_loop.record_decision(args.candidate_id, args.decision, reason=args.reason)
        _print(payload, args.json)
        return 0

    if not any([args.brief, args.brief_file, args.watchlist, args.fetch_watchlist]):
        parser.error("provide --brief, --brief-file, --watchlist, or --fetch-watchlist")

    while True:
        briefs, fetch_report = _cycle_inputs(args)
        payload = upgrade_loop.run_cycle(
            briefs,
            source=args.source,
            limit=args.limit,
            create_ticket_records=args.create_tickets,
            auto_promote=args.auto_promote,
            sandbox_test=args.sandbox_smoke,
        )
        if fetch_report:
            payload["fetch_report"] = fetch_report
        _print(payload, args.json)
        if not args.watch:
            return 0
        time.sleep(max(60, int(args.interval)))


if __name__ == "__main__":
    raise SystemExit(main())
