#!/usr/bin/env python3
"""
Talk to Jarvis from any terminal while it's running.

Usage:
  python jarvis_cli.py what's the weather
  python jarvis_cli.py remember I prefer dark mode
  python jarvis_cli.py --memory
  python jarvis_cli.py --status
  python jarvis_cli.py --doctor
  python jarvis_cli.py --permissions
  python jarvis_cli.py --skills
  python jarvis_cli.py --connectors
  python jarvis_cli.py --plugins
  python jarvis_cli.py --context-budget
  python jarvis_cli.py --agent-patterns
  python jarvis_cli.py --parity
  python jarvis_cli.py --capability-evals
  python jarvis_cli.py --security-roe
  python jarvis_cli.py --graph-query "meeting watchdog"
  python jarvis_cli.py --graph-path JarvisWindow _meeting_watchdog_tick
  python jarvis_cli.py --agents
  python jarvis_cli.py --tasks
  python jarvis_cli.py --task-status <task_id>
  python jarvis_cli.py --watch-task <task_id>
  python jarvis_cli.py --cancel-task <task_id>
  python jarvis_cli.py --approve-task <task_id>
  python jarvis_cli.py --deny-task <task_id>
  python jarvis_cli.py --task fix the login bug   # streaming, Multica-compatible
  python jarvis_cli.py --task-code refactor the auth middleware
  python jarvis_cli.py --code-ultra refactor the auth middleware
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


_CONSOLE_STATE = {"effort": "medium", "pending_shell": ""}
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
        venv_root = os.path.realpath(os.path.dirname(os.path.dirname(target)))
        current_prefix = os.path.realpath(getattr(sys, "prefix", ""))
    except OSError:
        return

    # A venv executable can be a symlink to the same base Python binary. Checking
    # only realpath(sys.executable) misses that case; sys.prefix tells us whether
    # Python actually activated the project environment.
    if current_prefix == venv_root:
        return

    if os.getenv("_JARVIS_CLI_REEXEC_ATTEMPTED", "").lower() in {"1", "true"}:
        return

    env = os.environ.copy()
    env["_JARVIS_CLI_REEXEC_ATTEMPTED"] = "1"
    os.execve(target, [target] + sys.argv, env)


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
        if os.getenv("JARVIS_CLI_BOOT_LOGS", "").lower() not in {"1", "true", "yes"}:
            os.environ.setdefault("JARVIS_QUIET_BOOT", "1")
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
    if task.get("status") == "waiting_approval":
        print(f"[task:{task_id}] waiting for approval")
        print(f"Reason            : {task.get('approval_reason') or 'approval required'}")
        print(f"Use /approve {task_id} to start it or /deny {task_id} to cancel it.")
        return 0
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


def watch_task(task_id: str) -> int:
    task_id = (task_id or "").strip()
    if not task_id:
        print("Usage: /watch <task_id>", file=sys.stderr)
        return 1

    try:
        task_payload = get(f"/tasks/{task_id}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Error: task not found: {task_id}", file=sys.stderr)
            return 1
        raise

    task = task_payload.get("task") or {}
    workspace = task.get("workspace") or {}
    if workspace.get("enabled") and workspace.get("worktree_path"):
        print(f"[workspace:{workspace.get('worktree_path')}]", flush=True)
    if task.get("status") == "waiting_approval":
        print(f"{task_id}: waiting for approval")
        print(f"Reason            : {task.get('approval_reason') or 'approval required'}")
        print(f"Use /approve {task_id} to start it or /deny {task_id} to cancel it.")
        return 0

    base = _base()
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
                elif obj.get("type") == "status":
                    status = obj.get("status")
                    if status:
                        print(f"\n[status:{status}]", flush=True)
                elif obj.get("type") == "done":
                    if obj.get("status") not in {"succeeded", "cancelled"}:
                        return 1
                elif "chunk" in obj:
                    print(obj["chunk"], end="", flush=True)
        print()
        task_state = get(f"/tasks/{task_id}")
        status = ((task_state.get("task") or {}).get("status") or "").lower()
        return 0 if status in {"", "succeeded", "cancelled"} else 1
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Error: task not found: {task_id}", file=sys.stderr)
            return 1
        raise
    except urllib.error.URLError:
        print(f"Error: lost connection while streaming task {task_id}", file=sys.stderr)
        return 1


def cancel_task(task_id: str) -> int:
    task_id = (task_id or "").strip()
    if not task_id:
        print("Usage: /cancel <task_id>", file=sys.stderr)
        return 1
    try:
        payload = post(f"/tasks/{task_id}/cancel", {})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Error: task not found: {task_id}", file=sys.stderr)
            return 1
        raise
    task = payload.get("task") or {}
    print(f"{task.get('id', task_id)}: {task.get('status', 'unknown')}")
    return 0


def approve_task(task_id: str) -> int:
    task_id = (task_id or "").strip()
    if not task_id:
        print("Usage: /approve <task_id>", file=sys.stderr)
        return 1
    try:
        payload = post(f"/tasks/{task_id}/approve", {})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Error: task not found: {task_id}", file=sys.stderr)
            return 1
        raise
    task = payload.get("task") or {}
    print(f"{task.get('id', task_id)}: {task.get('status', 'unknown')}")
    return 0


def deny_task(task_id: str) -> int:
    task_id = (task_id or "").strip()
    if not task_id:
        print("Usage: /deny <task_id>", file=sys.stderr)
        return 1
    try:
        payload = post(f"/tasks/{task_id}/deny", {})
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            print(f"Error: task not found: {task_id}", file=sys.stderr)
            return 1
        raise
    task = payload.get("task") or {}
    print(f"{task.get('id', task_id)}: {task.get('status', 'unknown')}")
    return 0


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
        approval = ""
        if task.get("status") == "waiting_approval":
            approval = f" [{task.get('approval_reason') or 'approval required'}]"
        print(f"{task['id']}: {lifecycle} {task['kind']} -> {task['assigned_agent_id']}{approval}")


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


def _print_context_budget() -> None:
    payload = get("/context-budget")
    print(payload.get("purpose") or "Context budget")
    models = payload.get("models") or {}
    if models:
        print(
            "Models : "
            f"default={models.get('default', 'unknown')} "
            f"coder={models.get('coder', 'unknown')} "
            f"reasoning={models.get('reasoning', 'unknown')}"
        )
    usage = payload.get("usage") or {}
    print(
        "Usage  : "
        f"total={usage.get('total_tokens', 0)} "
        f"local={usage.get('local_tokens', 0)} "
        f"cloud={usage.get('cloud_tokens', 0)} "
        f"calls={usage.get('call_count', 0)}"
    )
    print("Profiles")
    for name, profile in (payload.get("profiles") or {}).items():
        print(f"  {name}: {profile.get('best_for', '')} -> {profile.get('rule', '')}")
    print("Commands")
    for command, description in (payload.get("commands") or {}).items():
        print(f"  {command}: {description}")


def _print_agent_patterns(category: str = "") -> None:
    suffix = ""
    if category:
        suffix = "?" + urllib.parse.urlencode({"category": category})
    payload = get("/agent-patterns" + suffix)
    patterns = payload.get("patterns") or []
    print("External Agent Patterns")
    if payload.get("verdict_counts"):
        counts = ", ".join(f"{key}={value}" for key, value in sorted(payload["verdict_counts"].items()))
        print(f"Verdicts: {counts}")
    for item in patterns:
        useful = "; ".join((item.get("useful_patterns") or [])[:2])
        seams = ", ".join((item.get("jarvis_seams") or [])[:2])
        print(f"{item.get('id')}: {item.get('verdict')} [{item.get('category')}]")
        print(f"  use  : {useful}")
        print(f"  seams: {seams}")


def _print_capability_parity() -> None:
    payload = get("/capability-parity")
    print(payload.get("goal") or "Capability parity")
    print(f"Mode   : {payload.get('mode', 'unknown')}")
    print(f"Score  : {int(float(payload.get('score', 0.0)) * 100)}% ready")
    print(f"Next   : {payload.get('next_best_seam', 'unknown')}")
    print("Features")
    for feature in payload.get("features", []):
        print(f"  {feature.get('id')}: {feature.get('status')} -> {feature.get('local_equivalent')}")
        print(f"    next: {feature.get('next_gap')}")


def _print_capability_evals(group: str = "") -> None:
    suffix = ""
    if group:
        suffix = "?" + urllib.parse.urlencode({"group": group})
    payload = get("/capability-evals" + suffix)
    print(payload.get("purpose") or "Capability evals")
    print(f"Coverage: {int(float(payload.get('coverage_score', 0.0)) * 100)}%")
    print(f"Next    : {payload.get('next_best_seam', 'unknown')}")
    print(f"Run     : {payload.get('live_command', '')}")
    print("Cases")
    for case in payload.get("cases", []):
        checks = ", ".join((case.get("checks") or [])[:3])
        print(f"  {case.get('group')}/{case.get('id')}: {checks}")


def _print_security_roe(template: str = "") -> None:
    suffix = ""
    if template:
        suffix = "?" + urllib.parse.urlencode({"template": template})
    payload = get("/security-roe" + suffix)
    print(payload.get("purpose") or "Defensive security ROE")
    print(f"Mode   : {payload.get('mode', 'unknown')}")
    print("Templates")
    for item in payload.get("templates", []):
        print(f"  {item.get('id')}: {item.get('name')} -> {item.get('best_for')}")
        must = ", ".join((item.get("must_have") or [])[:4])
        print(f"    require: {must}")
    print("Guardrails")
    for guardrail in payload.get("guardrails", []):
        print(f"  - {guardrail}")


def _safe_get(path: str) -> dict:
    try:
        return get(path)
    except Exception as exc:
        return {"_error": str(exc)}


def _decision_text(result: dict) -> str:
    if result.get("ok"):
        return f"allowed [{result.get('rule', 'allowed')}]"
    reason = str(result.get("reason") or "").strip()
    suffix = f" — {reason}" if reason else ""
    return f"blocked [{result.get('rule', 'blocked')}]{suffix}"


def _permissions_profile_name() -> str:
    import behavior_hooks

    if not behavior_hooks.max_permissive_profile_enabled():
        return "default"
    if os.getenv("JARVIS_PERMISSIVE_ALLOW_PROTECTED_WRITES", "").strip().lower() in {"1", "true", "yes", "on"}:
        return "max-permissive + protected-writes"
    return "max-permissive"


def _set_permissions_mode(mode: str) -> int:
    normalized = (mode or "").strip().lower()
    if normalized == "default":
        os.environ["JARVIS_MAX_PERMISSIVE_LOCAL_PROFILE"] = "0"
        os.environ["JARVIS_PERMISSIVE_ALLOW_PROTECTED_WRITES"] = "0"
        print("Permissions set to default.")
        return 0
    if normalized == "max-permissive":
        os.environ["JARVIS_MAX_PERMISSIVE_LOCAL_PROFILE"] = "1"
        os.environ["JARVIS_PERMISSIVE_ALLOW_PROTECTED_WRITES"] = "0"
        print("Permissions set to max-permissive. Protected writes are still blocked.")
        return 0
    print("Usage: /permissions [default|max-permissive|protected-writes on|off]", file=sys.stderr)
    return 1


def _set_protected_writes(enabled: bool) -> int:
    import behavior_hooks

    if not behavior_hooks.max_permissive_profile_enabled():
        print("Enable max-permissive first: /permissions max-permissive", file=sys.stderr)
        return 1
    os.environ["JARVIS_PERMISSIVE_ALLOW_PROTECTED_WRITES"] = "1" if enabled else "0"
    if enabled:
        print("Protected writes enabled for this console process.")
    else:
        print("Protected writes disabled for this console process.")
    return 0


def _print_permissions() -> None:
    import behavior_hooks
    import safety_permissions

    cwd = os.getcwd()
    repo_write_target = os.path.join(cwd, ".jarvis_permissions_probe")

    print("Permissions")
    print(f"Profile           : {_permissions_profile_name()}")
    print(f"Shell (normal)    : {_decision_text(safety_permissions.can_run_shell('ls', cwd=cwd))}")
    print(f"Shell (protected) : {_decision_text(safety_permissions.can_run_shell('rm /etc/hosts', cwd=cwd))}")
    print(f"Shell (admin)     : {_decision_text(safety_permissions.can_run_shell('rm /etc/hosts', admin=True, cwd=cwd))}")
    print(f"Write (repo path) : {_decision_text(safety_permissions.can_write_file(repo_write_target, source='jarvis_cli_permissions'))}")
    print(f"Write (protected) : {_decision_text(safety_permissions.can_write_file('/etc/hosts', source='jarvis_cli_permissions'))}")
    print(f"Self-improve ok   : {_decision_text(safety_permissions.can_self_improve('router.py'))}")
    print(f"Self-improve stop : {_decision_text(safety_permissions.can_self_improve('secrets.py'))}")


def _print_doctor() -> None:
    status = _safe_get("/status")
    runtime_payload = _safe_get("/runtime/state")
    local_payload = _safe_get("/local/capabilities")
    memory_payload = _safe_get("/memory/status")
    vault_status = _safe_get("/vault")
    hook_payload = _safe_get("/hooks/status")
    cost_payload = _safe_get("/cost-policy")
    runtime = (runtime_payload or {}).get("state") or {}
    local = (local_payload or {}).get("capabilities") or {}
    memory_status = (memory_payload or {}).get("status") or {}
    hook_status = (hook_payload or {}).get("hooks") or {}
    cost_status = (cost_payload or {}).get("policy") or {}

    stt = local.get("stt") or {}
    tts = local.get("tts") or {}
    semantic = local.get("semantic_memory") or {}
    local_vision = status.get("local_vision") or {}
    managed = runtime.get("managed_runtime") or {}
    task_counts = managed.get("task_counts") or {}
    persistence = runtime.get("persistence") or {}
    persisted = persistence.get("persisted_api_endpoint") or {}
    blocked_hooks = int(hook_status.get("blocked_count", 0) or 0)

    findings: list[str] = []
    advisories: list[str] = []
    for label, payload in (
        ("status", status),
        ("runtime state", runtime_payload),
        ("local capabilities", local_payload),
        ("memory status", memory_payload),
        ("vault status", vault_status),
        ("hook status", hook_payload),
        ("cost policy", cost_payload),
    ):
        if payload.get("_error"):
            findings.append(f"{label} unavailable: {payload['_error']}")
    if not status.get("local_available"):
        findings.append("local model routing is unavailable")
    if str(local_vision.get("state") or "").lower() not in {"ready", "available", "ok"}:
        findings.append(f"local vision is {local_vision.get('state', 'unavailable')}")
    if not stt.get("local_available"):
        findings.append("local STT is unavailable")
    if not tts.get("ready"):
        findings.append("local TTS is not ready")
    if not semantic.get("index_ready"):
        findings.append("semantic memory index is not ready")
    if not memory_status.get("long_term_profile_ready"):
        findings.append("long-term memory profile is not consolidated")
    if blocked_hooks:
        advisories.append(f"{blocked_hooks} behavior-hook action(s) were blocked recently")
    if cost_status.get("hard_budget"):
        findings.append("cloud routing is over the hard budget")
    elif cost_status.get("budget_pressure"):
        findings.append("cloud routing is over the soft budget")

    print("Doctor")
    print(f"API               : {status.get('status', 'unknown').upper()} @ {status.get('api_host', '127.0.0.1')}:{status.get('api_port', 'unknown')}")
    print(f"Mode              : {str(status.get('mode', 'unknown')).upper()} | local={'yes' if status.get('local_available') else 'no'}")
    print(f"Vision            : {local_vision.get('state', 'unknown')} ({local_vision.get('selected_model') or local_vision.get('preferred_model') or 'no model'})")
    print(f"STT               : {stt.get('active_engine', 'unknown')} | local={'yes' if stt.get('local_available') else 'no'}")
    print(f"TTS               : {'ready' if tts.get('ready') else 'not ready'} | {tts.get('engine', 'unknown')}:{tts.get('voice', 'unknown')}")
    print(f"Semantic memory   : {semantic.get('retrieval_backend', 'unknown')} | indexed={semantic.get('entries_indexed', 0)} | ready={'yes' if semantic.get('index_ready') else 'no'}")
    print(f"Runtime           : total={task_counts.get('total', 0)} waiting={task_counts.get('waiting_approval', 0)} running={task_counts.get('running', 0)} queued={task_counts.get('queued', 0)} failed={task_counts.get('failed', 0)} cancelled={task_counts.get('cancelled', 0)}")
    print(f"Persisted API     : {persisted.get('base_url') or 'none'}")
    print(f"Vault             : docs={vault_status.get('doc_count', 0)} pages={vault_status.get('wiki_page_count', 0)} citation_ready={'yes' if vault_status.get('citation_ready') else 'no'}")
    print(f"Memory            : facts={memory_status.get('facts', 0)} projects={memory_status.get('projects', 0)} conversations={memory_status.get('conversation_summaries', 0)} long_term={'yes' if memory_status.get('long_term_profile_ready') else 'no'}")
    print(f"Hooks             : events={hook_status.get('event_count', 0)} blocked={blocked_hooks}")
    print(
        "Cost policy       : "
        f"soft={'yes' if cost_status.get('budget_pressure') else 'no'} "
        f"hard={'yes' if cost_status.get('hard_budget') else 'no'} "
        f"next={cost_status.get('training_action', 'none')}"
    )
    if findings:
        print("Findings          : " + "; ".join(findings))
    else:
        print("Findings          : no obvious runtime blockers")
    if advisories:
        print("Advisories        : " + "; ".join(advisories))


def _console_help() -> str:
    return "\n".join(
        [
            "Jarvis console commands:",
            "  /help                 Show this help",
            "  /status               Show daemon and runtime status",
            "  /doctor               Show runtime health and likely blockers",
            "  /permissions          Show current shell/write/self-improve gates",
            "  /permissions <mode>   Set mode: default | max-permissive",
            "  /permissions protected-writes on|off",
            "  /mode                 Show current routing mode",
            "  /mode <name>          Set mode: auto | local | cloud | open-source",
            "  /effort [level]       Show or set effort: low | medium | high | xhigh",
            "  /agents               List managed agents",
            "  /tasks [status]       List tasks, optionally filtered by lifecycle state",
            "  /task <prompt>        Run a managed task",
            "  /code <prompt>        Run an isolated coding task",
            "  /task-status <id>     Show one task payload",
            "  /watch <task_id>      Stream an existing task until completion",
            "  /cancel <task_id>     Request cancellation for a task",
            "  /approve <task_id>    Approve a managed task waiting for approval",
            "  /deny <task_id>       Deny a managed task waiting for approval",
            "  /memory               Show memory snapshot",
            "  /skills               List skills",
            "  /connectors           List connectors",
            "  /plugins              List plugins",
            "  /vault                Show vault status",
            "  /context-budget       Show local coding/token discipline",
            "  /tokens               Alias for /context-budget",
            "  /agent-patterns [id]  Show external repo patterns Jarvis can adapt",
            "  /parity               Show local frontier capability parity",
            "  /capability-evals [g] Show eval coverage for local capability claims",
            "  /security-roe [id]    Show defensive cybersecurity ROE templates",
            "  /run <command>        Run a local shell command",
            "  /approve              Run the pending risky shell command once",
            "  /deny                 Clear the pending risky shell command",
            "  !<command>            Shortcut for /run",
            "  /clear                Clear the terminal",
            "  /exit                 Quit the console",
            "",
            "Terse task aliases:",
            "  /task-lite <prompt>   Quick managed task with tighter output",
            "  /task-ultra <prompt>  Managed task with maximum compression",
            "  /code-lite <prompt>   Quick isolated coding task",
            "  /code-ultra <prompt>  Isolated coding task with maximum compression",
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


def _shell_command_needs_approval(command: str) -> bool:
    lower = (command or "").strip().lower()
    if not lower:
        return False
    risky_markers = (
        "sudo ",
        "rm ",
        "mv ",
        "cp ",
        "chmod ",
        "chown ",
        "ln ",
        "tee ",
        ">",
        ">>",
        "git push",
        "git reset",
        "git clean",
        "brew install",
        "brew uninstall",
        "pip install",
        "pip uninstall",
        "uv pip install",
        "npm install -g",
    )
    return any(marker in lower for marker in risky_markers)


def _shell_command_risk_reason(command: str) -> str:
    lower = (command or "").strip().lower()
    if "sudo " in lower:
        return "privileged command"
    if any(marker in lower for marker in ("rm ", "mv ", "cp ", "chmod ", "chown ", "ln ", "tee ", ">", ">>")):
        return "state-changing shell command"
    if any(marker in lower for marker in ("git push", "git reset", "git clean")):
        return "repo-changing command"
    if any(marker in lower for marker in ("brew install", "brew uninstall", "pip install", "pip uninstall", "uv pip install", "npm install -g")):
        return "environment-changing command"
    return "risky shell command"


def _run_shell_command(command: str, *, approved: bool = False) -> int:
    if not command.strip():
        print("Usage: /run <shell command>", file=sys.stderr)
        return 1
    if not approved and _shell_command_needs_approval(command):
        _CONSOLE_STATE["pending_shell"] = command
        print(f"Approval required: {_shell_command_risk_reason(command)}.")
        print("Use /approve to run once or /deny to cancel.")
        print(f"Pending command   : {command}")
        return 0

    _CONSOLE_STATE["pending_shell"] = ""
    import terminal

    result = terminal.run_command(command, cwd=os.getcwd())
    print(result)
    return 0 if not result.lower().startswith("error") and not result.lower().startswith("blocked") else 1


def _approve_pending_shell_command() -> int:
    pending = str(_CONSOLE_STATE.get("pending_shell") or "").strip()
    if not pending:
        print("No pending shell command.")
        return 0
    print(f"Approved          : {pending}")
    return _run_shell_command(pending, approved=True)


def _deny_pending_shell_command() -> int:
    pending = str(_CONSOLE_STATE.get("pending_shell") or "").strip()
    if not pending:
        print("No pending shell command.")
        return 0
    _CONSOLE_STATE["pending_shell"] = ""
    print(f"Cancelled pending shell command: {pending}")
    return 0


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
    if command == "approve":
        if args:
            return approve_task(args)
        return _approve_pending_shell_command()
    if command == "deny":
        if args:
            return deny_task(args)
        return _deny_pending_shell_command()
    if command == "status":
        _print_status()
        return 0
    if command == "doctor":
        _print_doctor()
        return 0
    if command == "permissions":
        if not args:
            _print_permissions()
            return 0
        if args in {"default", "max-permissive"}:
            return _set_permissions_mode(args)
        if args.startswith("protected-writes "):
            toggle = args.split(" ", 1)[1].strip().lower()
            if toggle in {"on", "off"}:
                return _set_protected_writes(toggle == "on")
        print("Usage: /permissions [default|max-permissive|protected-writes on|off]", file=sys.stderr)
        return 1
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
    if command in {"context-budget", "tokens"}:
        _print_context_budget()
        return 0
    if command in {"agent-patterns", "patterns"}:
        _print_agent_patterns(args)
        return 0
    if command in {"parity", "capability-parity"}:
        _print_capability_parity()
        return 0
    if command in {"capability-evals", "evals", "frontier-evals"}:
        _print_capability_evals(args)
        return 0
    if command in {"security-roe", "roe"}:
        _print_security_roe(args)
        return 0
    if command == "task":
        if not args:
            print("Usage: /task <task description>", file=sys.stderr)
            return 1
        return stream_task(args)
    if command in {"task-lite", "task-ultra"}:
        if not args:
            print(f"Usage: /{command} <task description>", file=sys.stderr)
            return 1
        terse_mode = "lite" if command == "task-lite" else "ultra"
        return stream_task(args, terse_mode=terse_mode)
    if command == "code":
        if not args:
            print("Usage: /code <task description>", file=sys.stderr)
            return 1
        return stream_task(args, kind="code", terse_mode="full", isolated_workspace=True)
    if command in {"code-lite", "code-ultra"}:
        if not args:
            print(f"Usage: /{command} <task description>", file=sys.stderr)
            return 1
        terse_mode = "lite" if command == "code-lite" else "ultra"
        return stream_task(args, kind="code", terse_mode=terse_mode, isolated_workspace=True)
    if command == "task-status":
        if not args:
            print("Usage: /task-status <task_id>", file=sys.stderr)
            return 1
        payload = get(f"/tasks/{args}")
        print(json.dumps(payload.get("task", {}), indent=2))
        return 0
    if command == "watch":
        return watch_task(args)
    if command == "cancel":
        return cancel_task(args)
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

    if flag in {"--task-lite", "--task-ultra"}:
        if len(sys.argv) < 3:
            print(f"Usage: python jarvis_cli.py {flag} <task description>", file=sys.stderr)
            sys.exit(1)
        task = " ".join(sys.argv[2:])
        terse_mode = "lite" if flag == "--task-lite" else "ultra"
        sys.exit(stream_task(task, terse_mode=terse_mode))

    if flag in {"--task-code-lite", "--task-code-ultra", "--code-lite", "--code-ultra"}:
        if len(sys.argv) < 3:
            print(f"Usage: python jarvis_cli.py {flag} <task description>", file=sys.stderr)
            sys.exit(1)
        task = " ".join(sys.argv[2:])
        terse_mode = "lite" if flag in {"--task-code-lite", "--code-lite"} else "ultra"
        sys.exit(stream_task(task, kind="code", terse_mode=terse_mode, isolated_workspace=True))

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

    if flag == "--doctor":
        _print_doctor()
        return

    if flag == "--permissions":
        if len(sys.argv) == 2:
            _print_permissions()
            return
        if len(sys.argv) == 3 and sys.argv[2] in {"default", "max-permissive"}:
            sys.exit(_set_permissions_mode(sys.argv[2]))
        if len(sys.argv) == 4 and sys.argv[2] == "protected-writes" and sys.argv[3] in {"on", "off"}:
            sys.exit(_set_protected_writes(sys.argv[3] == "on"))
        print("Usage: python jarvis_cli.py --permissions [default|max-permissive|protected-writes on|off]", file=sys.stderr)
        sys.exit(1)

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

    if flag in {"--context-budget", "--tokens"}:
        _print_context_budget()
        return

    if flag in {"--agent-patterns", "--patterns"}:
        category = sys.argv[2] if len(sys.argv) > 2 else ""
        _print_agent_patterns(category)
        return

    if flag in {"--parity", "--capability-parity"}:
        _print_capability_parity()
        return

    if flag in {"--capability-evals", "--evals", "--frontier-evals"}:
        group = sys.argv[2] if len(sys.argv) > 2 else ""
        _print_capability_evals(group)
        return

    if flag in {"--security-roe", "--roe"}:
        template = sys.argv[2] if len(sys.argv) > 2 else ""
        _print_security_roe(template)
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

    if flag == "--watch-task":
        if len(sys.argv) < 3:
            print("Usage: python jarvis_cli.py --watch-task <task_id>", file=sys.stderr)
            sys.exit(1)
        sys.exit(watch_task(sys.argv[2]))

    if flag == "--cancel-task":
        if len(sys.argv) < 3:
            print("Usage: python jarvis_cli.py --cancel-task <task_id>", file=sys.stderr)
            sys.exit(1)
        sys.exit(cancel_task(sys.argv[2]))

    if flag == "--approve-task":
        if len(sys.argv) < 3:
            print("Usage: python jarvis_cli.py --approve-task <task_id>", file=sys.stderr)
            sys.exit(1)
        sys.exit(approve_task(sys.argv[2]))

    if flag == "--deny-task":
        if len(sys.argv) < 3:
            print("Usage: python jarvis_cli.py --deny-task <task_id>", file=sys.stderr)
            sys.exit(1)
        sys.exit(deny_task(sys.argv[2]))

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
