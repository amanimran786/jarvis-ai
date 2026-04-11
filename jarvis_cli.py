#!/usr/bin/env python3
"""
Talk to Jarvis from any terminal while it's running.

Usage:
  python jarvis_cli.py what's the weather
  python jarvis_cli.py remember I prefer dark mode
  python jarvis_cli.py --memory
  python jarvis_cli.py --status
  python jarvis_cli.py --skills
  python jarvis_cli.py --connectors
  python jarvis_cli.py --plugins
  python jarvis_cli.py --graph-query "meeting watchdog"
  python jarvis_cli.py --graph-path JarvisWindow _meeting_watchdog_tick
  python jarvis_cli.py --agents
  python jarvis_cli.py --tasks
  python jarvis_cli.py --task-status <task_id>
  python jarvis_cli.py --task fix the login bug   # streaming, Multica-compatible
  python jarvis_cli.py --task-code refactor the auth middleware
  python jarvis_cli.py --teach "user prompt" "ideal Jarvis answer"
  python jarvis_cli.py -p fix the login bug        # alias for --task
"""

import sys
import json
import os
import urllib.request
import urllib.error
import urllib.parse


def _auth_headers() -> dict[str, str]:
    token = os.getenv("JARVIS_API_TOKEN", "").strip()
    if not token:
        try:
            import runtime_state
            metadata = runtime_state.read_api_endpoint() or {}
            token = str(metadata.get("token") or "").strip()
        except Exception:
            token = ""
    return {"Authorization": f"Bearer {token}"} if token else {}


def _base() -> str:
    explicit = os.getenv("JARVIS_API_BASE_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")

    try:
        import runtime_state
        discovered = runtime_state.discover_api_endpoint()
        if discovered:
            return str(discovered["base_url"]).rstrip("/")
    except Exception:
        pass

    return "http://127.0.0.1:8765"


def post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        _base() + path, data=data,
        headers={"Content-Type": "application/json", **_auth_headers()}
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _stream_chat(message: str) -> int:
    base = _base()
    data = json.dumps(
        {
            "message": message,
            "stream": True,
            "source": "cli_chat",
            "meta": {"client": "jarvis_cli"},
        }
    ).encode()
    req = urllib.request.Request(
        base + "/chat", data=data,
        headers={"Content-Type": "application/json", **_auth_headers()}
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "meta":
                    # Emit model tag so Multica can capture it
                    print(f"\n[model:{obj.get('model', 'unknown')}]", flush=True)
                elif "chunk" in obj:
                    print(obj["chunk"], end="", flush=True)
        print()  # trailing newline
        return 0
    except urllib.error.HTTPError as _http_err:
        print(f"Error: Jarvis returned HTTP {_http_err.code} — {_http_err.reason}. Check Jarvis logs.", file=sys.stderr)
        return 1
    except urllib.error.URLError:
        print("Error: Jarvis is not running. Start it with: python main.py", file=sys.stderr)
        return 1


def stream_task(message: str, *, kind: str = "task", terse_mode: str = "full", isolated_workspace: bool | None = None) -> int:
    """Stream a managed task via the daemon-backed task runtime."""
    base = _base()
    payload = {
        "prompt": message,
        "kind": kind,
        "source": "cli_task",
        "terse_mode": terse_mode,
        "isolated_workspace": isolated_workspace,
        "meta": {"client": "jarvis_cli"},
    }
    try:
        created = post("/tasks", payload)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _stream_chat(message)
        print(f"Error: task submission failed ({exc.code})", file=sys.stderr)
        return 1
    except urllib.error.HTTPError as _http_err:
        print(f"Error: Jarvis returned HTTP {_http_err.code} — {_http_err.reason}. Check Jarvis logs.", file=sys.stderr)
        return 1
    except urllib.error.URLError:
        print("Error: Jarvis is not running. Start it with: python main.py", file=sys.stderr)
        return 1

    task = created.get("task") or {}
    task_id = task.get("id", "")
    if not task_id:
        print("Error: Jarvis did not return a task id.", file=sys.stderr)
        return 1

    workspace = task.get("workspace") or {}
    if workspace.get("enabled") and workspace.get("worktree_path"):
        print(f"[workspace:{workspace.get('worktree_path')}]", flush=True)
    try:
        req = urllib.request.Request(base + f"/tasks/{task_id}/stream", headers=_auth_headers())
        with urllib.request.urlopen(req, timeout=600) as resp:
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "meta":
                    print(f"\n[model:{obj.get('model', 'unknown')}]", flush=True)
                elif obj.get("type") == "error":
                    print(f"\n[error:{obj.get('error', 'task_failed')}]", flush=True)
                elif obj.get("type") == "done":
                    if obj.get("status") not in {"succeeded", "cancelled"}:
                        return 1
                elif "chunk" in obj:
                    print(obj["chunk"], end="", flush=True)
        print()
        task_state = get(f"/tasks/{task_id}")
        status = ((task_state.get("task") or {}).get("status") or "").lower()
        return 0 if status in {"", "succeeded", "cancelled"} else 1
    except urllib.error.URLError:
        print(f"Error: lost connection while streaming task {task_id}", file=sys.stderr)
        return 1


def get(path: str) -> dict:
    req = urllib.request.Request(_base() + path, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def teach(prompt: str, answer: str) -> dict:
    return post(
        "/local/training/teach",
        {
            "prompt": prompt,
            "answer": answer,
            "source": "manual_teacher",
            "tags": ["cli", "codex"],
            "meta": {"client": "jarvis_cli"},
        },
    )


def main():
    if len(sys.argv) < 2:
        print("Usage: python jarvis_cli.py <message>")
        print("       python jarvis_cli.py --status")
        print("       python jarvis_cli.py --memory")
        print("       python jarvis_cli.py --skills")
        print("       python jarvis_cli.py --skill <skill_id>")
        print("       python jarvis_cli.py --connectors")
        print("       python jarvis_cli.py --connector <connector_id>")
        print("       python jarvis_cli.py --plugins")
        print("       python jarvis_cli.py --plugin <plugin_id>")
        print("       python jarvis_cli.py --graph-query <query>")
        print("       python jarvis_cli.py --graph-path <source> <target>")
        print("       python jarvis_cli.py --agents")
        print("       python jarvis_cli.py --tasks")
        print("       python jarvis_cli.py --task-status <task_id>")
        print("       python jarvis_cli.py --task <task>   (streaming, Multica-compatible)")
        print("       python jarvis_cli.py --task-code <task>   (isolated coding task)")
        print('       python jarvis_cli.py --teach "<prompt>" "<ideal answer>"')
        print("       python jarvis_cli.py -p <task>       (alias for --task)")
        sys.exit(1)

    flag = sys.argv[1]

    if flag in ("--task", "-p"):
        if len(sys.argv) < 3:
            print("Usage: python jarvis_cli.py --task <task description>", file=sys.stderr)
            sys.exit(1)
        task = " ".join(sys.argv[2:])
        sys.exit(stream_task(task))

    if flag == "--task-code":
        if len(sys.argv) < 3:
            print("Usage: python jarvis_cli.py --task-code <task description>", file=sys.stderr)
            sys.exit(1)
        task = " ".join(sys.argv[2:])
        sys.exit(stream_task(task, kind="code", terse_mode="full", isolated_workspace=True))

    if flag == "--teach":
        if len(sys.argv) != 4:
            print('Usage: python jarvis_cli.py --teach "<prompt>" "<ideal answer>"', file=sys.stderr)
            sys.exit(1)
        result = teach(sys.argv[2], sys.argv[3])
        print(result.get("message") or json.dumps(result))
        sys.exit(0 if result.get("ok") else 1)

    if flag == "--status":
        s = get("/status")
        print(f"Status : {s['status'].upper()}")
        print(f"Mode   : {s['mode'].upper()}")
        print(f"Local  : {'available' if s['local_available'] else 'not available'}")
        return

    if flag == "--skills":
        payload = get("/skills")
        for skill in payload.get("skills", []):
            print(f"{skill['id']}: {skill['tool']} [{skill['cost_hint']}]")
            print(f"  {skill['description']}")
        return

    if flag == "--skill":
        if len(sys.argv) < 3:
            print("Usage: python jarvis_cli.py --skill <skill_id>", file=sys.stderr)
            sys.exit(1)
        payload = get(f"/skills/{sys.argv[2]}")
        print(json.dumps(payload.get("skill", {}), indent=2))
        return

    if flag == "--connectors":
        payload = get("/connectors")
        for connector in payload.get("connectors", []):
            scope = "local" if connector.get("local_only") else "hybrid"
            print(f"{connector['id']}: {connector['transport']} [{scope}]")
            print(f"  {connector['description']}")
        return

    if flag == "--connector":
        if len(sys.argv) < 3:
            print("Usage: python jarvis_cli.py --connector <connector_id>", file=sys.stderr)
            sys.exit(1)
        payload = get(f"/connectors/{sys.argv[2]}")
        print(json.dumps(payload.get("connector", {}), indent=2))
        return

    if flag == "--plugins":
        payload = get("/plugins")
        for plugin in payload.get("plugins", []):
            scope = "local" if plugin.get("local_only") else "hybrid"
            print(f"{plugin['id']}: {plugin['category']} [{scope}]")
            print(f"  {plugin['description']}")
        return

    if flag == "--plugin":
        if len(sys.argv) < 3:
            print("Usage: python jarvis_cli.py --plugin <plugin_id>", file=sys.stderr)
            sys.exit(1)
        payload = get(f"/plugins/{sys.argv[2]}")
        print(json.dumps(payload.get("plugin", {}), indent=2))
        return

    if flag == "--graph-query":
        if len(sys.argv) < 3:
            print("Usage: python jarvis_cli.py --graph-query <query>", file=sys.stderr)
            sys.exit(1)
        query = " ".join(sys.argv[2:])
        payload = get(f"/graph/query?q={urllib.parse.quote(query)}")
        print(json.dumps(payload.get("result", {}), indent=2))
        return

    if flag == "--graph-path":
        if len(sys.argv) < 4:
            print("Usage: python jarvis_cli.py --graph-path <source> <target>", file=sys.stderr)
            sys.exit(1)
        source = urllib.parse.quote(sys.argv[2])
        target = urllib.parse.quote(sys.argv[3])
        payload = get(f"/graph/path?source={source}&target={target}")
        print(json.dumps(payload.get("result", {}), indent=2))
        return

    if flag == "--agents":
        payload = get("/agents")
        for agent in payload.get("agents", []):
            print(f"{agent['id']}: {agent['status']} [{', '.join(agent.get('capabilities', []))}]")
        return

    if flag == "--tasks":
        payload = get("/tasks")
        for task in payload.get("tasks", []):
            print(f"{task['id']}: {task['status']} {task['kind']} -> {task['assigned_agent_id']}")
        return

    if flag == "--task-status":
        if len(sys.argv) < 3:
            print("Usage: python jarvis_cli.py --task-status <task_id>", file=sys.stderr)
            sys.exit(1)
        payload = get(f"/tasks/{sys.argv[2]}")
        task = payload.get("task", {})
        print(json.dumps(task, indent=2))
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
        result = post("/chat", {"message": message, "source": "cli_chat", "meta": {"client": "jarvis_cli"}})
        print(f"[{result['model']}] {result['response']}")
    except urllib.error.HTTPError as _http_err:
        print(f"Error: Jarvis returned HTTP {_http_err.code} — check Jarvis logs.")
        sys.exit(1)
    except urllib.error.URLError:
        print("Error: Jarvis is not running. Start it with: python main.py")
        sys.exit(1)


if __name__ == "__main__":
    main()
