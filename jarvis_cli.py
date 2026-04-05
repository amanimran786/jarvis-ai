#!/usr/bin/env python3
"""
Talk to Jarvis from any terminal while it's running.

Usage:
  python jarvis_cli.py what's the weather
  python jarvis_cli.py remember I prefer dark mode
  python jarvis_cli.py --memory
  python jarvis_cli.py --status
"""

import sys
import json
import urllib.request
import urllib.error

def _base() -> str:
    import os
    port_file = os.path.join(os.path.dirname(__file__), ".jarvis_port")
    try:
        with open(port_file) as f:
            port = f.read().strip()
        return f"http://127.0.0.1:{port}"
    except FileNotFoundError:
        return "http://127.0.0.1:8765"

BASE = _base()


def post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def get(path: str) -> dict:
    with urllib.request.urlopen(BASE + path, timeout=10) as resp:
        return json.loads(resp.read())


def main():
    if len(sys.argv) < 2:
        print("Usage: python jarvis_cli.py <message>")
        print("       python jarvis_cli.py --status")
        print("       python jarvis_cli.py --memory")
        sys.exit(1)

    flag = sys.argv[1]

    if flag == "--status":
        s = get("/status")
        print(f"Status : {s['status'].upper()}")
        print(f"Mode   : {s['mode'].upper()}")
        print(f"Local  : {'available' if s['local_available'] else 'not available'}")
        return

    if flag == "--memory":
        m = get("/memory")
        print("── Facts ───────────────────────────────")
        for f in m["facts"] or ["(none)"]:
            print(f"  • {f}")
        print("── Top Topics ──────────────────────────")
        print(f"  {', '.join(m['top_topics']) or '(none)'}")
        print("── Recent Conversations ────────────────")
        for c in m["recent_conversations"]:
            print(f"  [{c['date']}] {c['summary']}")
        return

    message = " ".join(sys.argv[1:])
    try:
        result = post("/chat", {"message": message})
        print(f"[{result['model']}] {result['response']}")
    except urllib.error.URLError:
        print("Error: Jarvis is not running. Start it with: python main.py")
        sys.exit(1)


if __name__ == "__main__":
    main()
