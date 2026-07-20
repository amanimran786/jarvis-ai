#!/usr/bin/env python3
"""Jarvis Plugin Manager CLI
Usage:
  python3 plugin_manager.py           # list all plugins
  python3 plugin_manager.py test      # fire test events
  python3 plugin_manager.py enable <name>
  python3 plugin_manager.py disable <name>
"""
import sys
from pathlib import Path
BASE = Path(__file__).parent
sys.path.insert(0, str(BASE))
from plugins.loader import PluginLoader

def main():
    loader = PluginLoader(BASE)
    loader.load_all()
    print()
    print("=" * 60)
    print(loader.summary())
    print("=" * 60)

    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"

    if cmd == "enable" and len(sys.argv) > 2:
        name = sys.argv[2]
        print(f"{'Enabled' if loader.enable(name) else 'Not found'}: {name}")
    elif cmd == "disable" and len(sys.argv) > 2:
        name = sys.argv[2]
        print(f"{'Disabled' if loader.disable(name) else 'Not found'}: {name}")
    elif cmd == "test":
        print()
        print("=== Test Run ===")
        task = {"id": "test-001", "title": "write API documentation", "status": "queued"}
        task = loader.fire_task_queued(task)
        print(f"on_task_queued  => domain={task.get('domain')!r}")
        task["status"] = "done"
        loader.fire_task_completed(task)
        print("on_task_completed => fired (check plugins/task_log.jsonl)")
        widgets = loader.collect_dashboard_widgets()
        print(f"dashboard_widget  => {len(widgets)} widget(s)")
        msg = loader.fire_message("hello jarvis", {})
        print(f"fire_message      => {msg!r}")
        print()
        print("All hooks OK!")

    widgets = loader.collect_dashboard_widgets()
    if widgets:
        print(f"({len(widgets)} dashboard widget(s) registered)")

    input("\nPress Enter to close...")

if __name__ == "__main__":
    main()
