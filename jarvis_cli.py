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
import atexit
import urllib.request
import urllib.error
import urllib.parse

try:
    import readline  # noqa: F401
except Exception:
    readline = None


_CONSOLE_STATE = {"effort": "medium"}
_OWNS_DAEMON = False
_DAEMON_CLEANUP_REGISTERED = False


def _project_venv_python() -> str:
    return os.path.join(os.path.dirname(__file__), "venv", "bin", "python")


def _ensure_supported_cli_runtime() -> None:
    if getattr(sys, "frozen", False):
        return

    target = _project_venv_python()
    if not os.path.exists(target):
        return

    try:
        current = os.path.realpath(sys.executable)
        target_real = os.path.realpath(target)
    except OSError:
        return

    if current == target_real:
        return

    if os.getenv("_JARVIS_CLI_REEXEC_ATTEMPTED", "").lower() in {"1", "true"}:
        return

    env = os.environ.copy()
    env["_JARVIS_CLI_REEXEC_ATTEMPTED"] = "1"
    os.execve(target_real, [target_real] + sys.argv, env)


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


def _clear_owned_daemon_state() -> None:
    global _OWNS_DAEMON
    if not _OWNS_DAEMON:
        return
    try:
        import runtime_state
        runtime_state.mark_stopped("jarvis_cli_exit")
        runtime_state.clear_api_endpoint()
        runtime_state.clear_console_session()
    except Exception:
        pass
    _OWNS_DAEMON = False


def _register_owned_daemon_cleanup() -> None:
    global _DAEMON_CLEANUP_REGISTERED
    if _DAEMON_CLEANUP_REGISTERED:
        return
    atexit.register(_clear_owned_daemon_state)
    _DAEMON_CLEANUP_REGISTERED = True


def _ensure_daemon_running(reason: str = "jarvis_cli") -> bool:
    global _OWNS_DAEMON
    try:
        import runtime_state
        discovered = runtime_state.discover_api_endpoint()
        if discovered:
            return True
        if runtime_state.read_api_endpoint():
            runtime_state.clear_api_endpoint()
        import jarvis_daemon
        jarvis_daemon.start_daemon(reason=reason)
        discovered = runtime_state.discover_api_endpoint()
        if not discovered:
            return False
        persisted = runtime_state.read_api_endpoint() or {}
        if persisted.get("pid") == os.getpid():
            _OWNS_DAEMON = True
            _register_owned_daemon_cleanup()
        return True
    except Exception:
        return False


def post(path: str, body: dict) -> dict:
    _ensure_daemon_running(reason="jarvis_cli_post")
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
            "meta": {"client": "jarvis_cli", "effort": _CONSOLE_STATE["effort"]},
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
        "meta": {"client": "jarvis_cli", "effort": _CONSOLE_STATE["effort"]},
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
    _ensure_daemon_running(reason="jarvis_cli_get")
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


def _print_status() -> None:
    s = get("/status")
    managed = s.get("managed_runtime_summary") or {}
    lifecycle = managed.get("lifecycle_counts") or {}
    print(f"Status : {s['status'].upper()}")
    print(f"Mode   : {s['mode'].upper()}")
    print(f"Local  : {'available' if s['local_available'] else 'not available'}")
    if lifecycle:
        print(
            "Tasks  : "
            f"Q={lifecycle.get('queued', 0)} "
            f"C={lifecycle.get('claimed', 0)} "
            f"R={lifecycle.get('running', 0)} "
            f"B={lifecycle.get('blocked', 0)} "
            f"D={lifecycle.get('completed', 0)}"
        )


def _print_agents() -> None:
    payload = get("/agents")
    for agent in payload.get("agents", []):
        caps = ", ".join(agent.get("capabilities", []))
        lifecycle = agent.get("lifecycle_state") or agent.get("status")
        print(f"{agent['id']}: {lifecycle} [{caps}]")


def _print_tasks(status: str = "") -> None:
    path = "/tasks"
    if status:
        path += "?" + urllib.parse.urlencode({"status": status})
    payload = get(path)
    for task in payload.get("tasks", []):
        lifecycle = task.get("lifecycle_state") or task.get("status")
        print(f"{task['id']}: {lifecycle} {task['kind']} -> {task['assigned_agent_id']}")


def _print_memory() -> None:
    m = get("/memory")
    print("── Facts ───────────────────────────────")
    for f in m["facts"] or ["(none)"]:
        print(f"  • {f}")
    print("── Top Topics ──────────────────────────")
    print(f"  {', '.join(m['top_topics']) or '(none)'}")
    print("── Recent Conversations ────────────────")
    for c in m["recent_conversations"]:
        print(f"  [{c['date']}] {c['summary']}")


def _print_skills() -> None:
    payload = get("/skills")
    for skill in payload.get("skills", []):
        print(f"{skill['id']}: {skill['tool']} [{skill['cost_hint']}]")
        print(f"  {skill['description']}")


def _print_connectors() -> None:
    payload = get("/connectors")
    for connector in payload.get("connectors", []):
        scope = "local" if connector.get("local_only") else "hybrid"
        print(f"{connector['id']}: {connector['transport']} [{scope}]")
        print(f"  {connector['description']}")


def _print_plugins() -> None:
    payload = get("/plugins")
    for plugin in payload.get("plugins", []):
        scope = "local" if plugin.get("local_only") else "hybrid"
        print(f"{plugin['id']}: {plugin['category']} [{scope}]")
        print(f"  {plugin['description']}")


def _print_vault_status() -> None:
    payload = get("/vault")
    print(json.dumps(payload, indent=2))


def _console_help() -> str:
    return "\n".join(
        [
            "Jarvis console commands:",
            "  /help                 Show this help",
            "  /status               Show daemon and runtime status",
            "  /mode                 Show current routing mode",
            "  /mode <name>          Set mode: auto | local | cloud | open-source",
            "  /effort [level]       Show or set effort: low | medium | high | xhigh",
            "  /agents               List managed agents",
            "  /tasks [status]       List tasks, optionally filtered by lifecycle state",
            "  /task <prompt>        Run a managed task",
            "  /code <prompt>        Run an isolated coding task",
            "  /task-status <id>     Show one task payload",
            "  /memory               Show memory snapshot",
            "  /skills               List skills",
            "  /connectors           List connectors",
            "  /plugins              List plugins",
            "  /vault                Show vault status",
            "  /run <command>        Run a local shell command",
            "  !<command>            Shortcut for /run",
            "  /clear                Clear the terminal",
            "  /exit                 Quit the console",
            "",
            "Any non-slash input is sent as a normal chat message.",
        ]
    )


def _banner_text() -> str:
    status = "unknown"
    mode = "unknown"
    try:
        payload = get("/status")
        status = str(payload.get("status", "unknown")).upper()
        mode = str(payload.get("mode", "unknown")).upper()
    except Exception:
        pass
    return "\n".join(
        [
            "Jarvis Console",
            f"Mode: {mode}   Effort: {_CONSOLE_STATE['effort']}   Status: {status}",
            os.getcwd(),
            "Type /help for commands.",
        ]
    )


def _set_mode(mode: str) -> int:
    result = post("/mode", {"mode": mode})
    print(result.get("message") or result.get("mode") or "mode updated")
    return 0 if result.get("ok", True) else 1


def _set_effort(level: str) -> int:
    normalized = (level or "").strip().lower()
    if normalized not in {"low", "medium", "high", "xhigh"}:
        print("Usage: /effort <low|medium|high|xhigh>", file=sys.stderr)
        return 1
    _CONSOLE_STATE["effort"] = normalized
    print(f"Effort set to {normalized}.")
    return 0


def _run_shell_command(command: str) -> int:
    if not command.strip():
        print("Usage: /run <shell command>", file=sys.stderr)
        return 1
    import terminal

    result = terminal.run_command(command, cwd=os.getcwd())
    print(result)
    return 0 if not result.lower().startswith("error") and not result.lower().startswith("blocked") else 1


def _handle_console_command(line: str) -> int | None:
    text = (line or "").strip()
    if not text:
        return 0
    if text.startswith("!"):
        return _run_shell_command(text[1:].strip())
    if not text.startswith("/"):
        return _stream_chat(text)

    command, _, raw_args = text[1:].partition(" ")
    command = command.strip().lower()
    args = raw_args.strip()

    if command in {"exit", "quit"}:
        return None
    if command == "help":
        print(_console_help())
        return 0
    if command == "clear":
        print("\033c", end="")
        return 0
    if command == "status":
        _print_status()
        return 0
    if command == "mode":
        if not args:
            payload = get("/mode")
            print(payload.get("mode", "unknown"))
            return 0
        return _set_mode(args)
    if command == "effort":
        if not args:
            print(f"Current effort: {_CONSOLE_STATE['effort']}")
            return 0
        return _set_effort(args)
    if command == "agents":
        _print_agents()
        return 0
    if command == "tasks":
        _print_tasks(args)
        return 0
    if command == "task":
        if not args:
            print("Usage: /task <task description>", file=sys.stderr)
            return 1
        return stream_task(args)
    if command == "code":
        if not args:
            print("Usage: /code <task description>", file=sys.stderr)
            return 1
        return stream_task(args, kind="code", terse_mode="full", isolated_workspace=True)
    if command == "task-status":
        if not args:
            print("Usage: /task-status <task_id>", file=sys.stderr)
            return 1
        payload = get(f"/tasks/{args}")
        print(json.dumps(payload.get("task", {}), indent=2))
        return 0
    if command == "memory":
        _print_memory()
        return 0
    if command == "skills":
        _print_skills()
        return 0
    if command == "connectors":
        _print_connectors()
        return 0
    if command == "plugins":
        _print_plugins()
        return 0
    if command == "vault":
        _print_vault_status()
        return 0
    if command == "run":
        return _run_shell_command(args)

    print(f"Unknown command: /{command}. Use /help.", file=sys.stderr)
    return 1


def run_interactive_console() -> int:
    if not _ensure_daemon_running(reason="jarvis_cli_console"):
        print("Error: Jarvis could not start its local daemon.", file=sys.stderr)
        return 1
    try:
        import runtime_state
        runtime_state.write_console_session(command="jarvis_cli --interactive")
        atexit.register(runtime_state.clear_console_session)
    except Exception:
        pass
    print(_banner_text())
    while True:
        try:
            line = input("› ")
        except EOFError:
            print()
            return 0
        except KeyboardInterrupt:
            print("\nUse /exit to quit.")
            continue
        try:
            result = _handle_console_command(line)
        except urllib.error.HTTPError as exc:
            print(f"Error: Jarvis returned HTTP {exc.code} — {exc.reason}.", file=sys.stderr)
            continue
        except urllib.error.URLError:
            print("Error: Jarvis is not running. Start it with: python main.py", file=sys.stderr)
            continue
        if result is None:
            return 0


def main():
    _ensure_supported_cli_runtime()

    if len(sys.argv) < 2:
        sys.exit(run_interactive_console())

    flag = sys.argv[1]

    if flag in {"--interactive", "-i", "--console"}:
        sys.exit(run_interactive_console())

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
        _print_status()
        return

    if flag == "--skills":
        _print_skills()
        return

    if flag == "--skill":
        if len(sys.argv) < 3:
            print("Usage: python jarvis_cli.py --skill <skill_id>", file=sys.stderr)
            sys.exit(1)
        payload = get(f"/skills/{sys.argv[2]}")
        print(json.dumps(payload.get("skill", {}), indent=2))
        return

    if flag == "--connectors":
        _print_connectors()
        return

    if flag == "--connector":
        if len(sys.argv) < 3:
            print("Usage: python jarvis_cli.py --connector <connector_id>", file=sys.stderr)
            sys.exit(1)
        payload = get(f"/connectors/{sys.argv[2]}")
        print(json.dumps(payload.get("connector", {}), indent=2))
        return

    if flag == "--plugins":
        _print_plugins()
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
        _print_agents()
        return

    if flag == "--tasks":
        _print_tasks()
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
        _print_memory()
        return

    message = " ".join(sys.argv[1:])
    try:
        _ensure_daemon_running(reason="jarvis_cli_oneshot")
        result = post("/chat", {"message": message, "source": "cli_chat", "meta": {"client": "jarvis_cli", "effort": _CONSOLE_STATE["effort"]}})
        print(f"[{result['model']}] {result['response']}")
    except urllib.error.HTTPError as _http_err:
        print(f"Error: Jarvis returned HTTP {_http_err.code} — check Jarvis logs.")
        sys.exit(1)
    except urllib.error.URLError:
        print("Error: Jarvis is not running. Start it with: python main.py")
        sys.exit(1)


if __name__ == "__main__":
    main()
