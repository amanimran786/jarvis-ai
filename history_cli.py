#!/usr/bin/env python3
"""
Jarvis /history — Rich CLI viewer for task history and work queue.
Usage: python3 ~/jarvis-ai/history_cli.py [--status pending|done|failed|all] [--limit N]
"""

import json
import sys
import argparse
from pathlib import Path

BASE = Path.home() / "jarvis-ai"


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def fmt_date(d):
    if not d:
        return "—"
    return str(d)[:16]


def status_icon(s):
    icons = {
        "pending": "⏳",
        "queued": "⏳",
        "running": "🔄",
        "active": "🔄",
        "done": "✅",
        "completed": "✅",
        "failed": "❌",
        "stalled": "⚠️",
        "fired": "🚀",
    }
    return icons.get(str(s).lower(), "❓")


def try_rich(tasks, status_filter, limit):
    """Attempt to render with rich library."""
    from rich.console import Console
    from rich.table import Table
    from rich.panel import Panel
    from rich.text import Text
    from rich import box

    console = Console()

    total = len(tasks)
    by_status = {}
    for t in tasks:
        s = t.get("status", "?")
        by_status[s] = by_status.get(s, 0) + 1

    stats_parts = "  ".join(
        f"{status_icon(s)} {s}: {c}" for s, c in sorted(by_status.items())
    )
    console.print(Panel(
        f"[bold cyan]⚡ Jarvis Task History[/bold cyan]\n"
        f"[dim]{total} total tasks[/dim]\n"
        f"{stats_parts}",
        border_style="cyan",
        title="[bold]JARVIS AI[/bold]",
        subtitle=f"[dim]{BASE}/WORK_QUEUE.json[/dim]",
    ))

    if status_filter == "all":
        filtered = tasks
    else:
        filtered = [t for t in tasks if t.get("status", "").lower() == status_filter.lower()]

    if limit:
        filtered = filtered[-limit:]

    if not filtered:
        console.print(f"[yellow]No tasks matching status: {status_filter}[/yellow]")
        return

    STATUS_STYLES = {
        "pending": "yellow",
        "queued": "yellow",
        "running": "bright_blue",
        "active": "bright_blue",
        "done": "bright_green",
        "completed": "bright_green",
        "failed": "bright_red",
        "stalled": "bright_red",
        "fired": "magenta",
    }

    table = Table(
        box=box.ROUNDED,
        show_header=True,
        header_style="bold cyan",
        row_styles=["", "dim"],
        expand=True,
    )
    table.add_column("#", style="dim", width=6, no_wrap=True)
    table.add_column("ID", style="dim", width=10, no_wrap=True)
    table.add_column("Title", min_width=28, ratio=2)
    table.add_column("Status", width=14, no_wrap=True)
    table.add_column("AI", width=14, no_wrap=True)
    table.add_column("Domain", width=12, no_wrap=True)
    table.add_column("Created", width=16, no_wrap=True)

    for i, t in enumerate(filtered, 1):
        s = t.get("status", "?")
        style = STATUS_STYLES.get(s.lower(), "white")
        table.add_row(
            str(i),
            str(t.get("id", ""))[:10],
            t.get("title", ""),
            Text(f"{status_icon(s)} {s}", style=style),
            str(t.get("assigned_ai", "—")),
            str(t.get("domain", "")),
            fmt_date(t.get("created_at", "")),
        )

    console.print(table)
    console.print(
        f"\n[dim]Showing [bold]{len(filtered)}[/bold] of [bold]{total}[/bold] tasks"
        + (f" · filter: [yellow]{status_filter}[/yellow]" if status_filter != "all" else "")
        + (f" · last {limit}" if limit else "")
        + "[/dim]"
    )


def plain_render(tasks, status_filter, limit):
    """Fallback plain-text rendering."""
    total = len(tasks)

    if status_filter == "all":
        filtered = tasks
    else:
        filtered = [t for t in tasks if t.get("status", "").lower() == status_filter.lower()]

    if limit:
        filtered = filtered[-limit:]

    print(f"\n⚡ Jarvis Task History — {total} total")
    print("=" * 80)
    print(f"{'#':<4} {'ID':<12} {'Status':<12} {'AI':<16} {'Title'}")
    print("-" * 80)

    for i, t in enumerate(filtered, 1):
        s = t.get("status", "?")
        icon = status_icon(s)
        title = t.get("title", "")[:40]
        print(f"{i:<4} {str(t.get('id',''))[:12]:<12} {icon}{s:<11} {str(t.get('assigned_ai','—'))[:16]:<16} {title}")

    print("-" * 80)
    print(f"Showing {len(filtered)} of {total} tasks | filter: {status_filter}")


def main():
    parser = argparse.ArgumentParser(
        description="Jarvis task history viewer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 history_cli.py                    # show all tasks
  python3 history_cli.py --status pending   # only pending
  python3 history_cli.py --status done --limit 20   # last 20 done
  python3 history_cli.py --plain            # plain text output
        """,
    )
    parser.add_argument(
        "--status",
        default="all",
        choices=["all", "pending", "queued", "running", "active", "done", "completed", "failed", "stalled", "fired"],
        help="Filter tasks by status (default: all)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Show last N tasks, 0 = show all (default: 0)",
    )
    parser.add_argument(
        "--plain",
        action="store_true",
        help="Plain text output without rich formatting",
    )
    args = parser.parse_args()

    tasks = load_json(BASE / "WORK_QUEUE.json", [])

    if not tasks:
        print("⚠️  No tasks found in WORK_QUEUE.json")
        sys.exit(0)

    if args.plain:
        plain_render(tasks, args.status, args.limit)
    else:
        try:
            try_rich(tasks, args.status, args.limit)
        except ImportError:
            print("(rich not installed — pip install rich — using plain output)\n")
            plain_render(tasks, args.status, args.limit)

    input("\nPress Enter to close...")


if __name__ == "__main__":
    main()
