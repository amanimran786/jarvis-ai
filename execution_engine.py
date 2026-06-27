from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

log = logging.getLogger("jarvis.execution_engine")
from pathlib import Path

from brains.brain_claude import ask_claude
from config import DEFAULT_MODE, LOCAL_DEFAULT, SONNET
import skills
import tool_registry
import runtime_state
from task_planner import TaskStep


TRACE_DIR = runtime_state.writable_data_path("training", "execution_traces")
DEFAULT_MALWARE_API_BASE = os.getenv("JARVIS_MALWARE_API_BASE", "http://127.0.0.1:9100").strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_slug(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value).strip("-") or "step"


def _ensure_trace_dir() -> None:
    TRACE_DIR.mkdir(parents=True, exist_ok=True)


def resolve_params(params: dict, step_results: dict[int, str]) -> dict:
    resolved = {}
    for key, value in dict(params or {}).items():
        if isinstance(value, str) and value.startswith("$step_"):
            match = re.match(r"\$step_(\d+)_result", value)
            if match:
                value = step_results.get(int(match.group(1)), "")
        resolved[key] = value
    return resolved


def _verify_result(spec: tool_registry.ToolSpec, result: str) -> tuple[bool, str]:
    text = (result or "").strip()
    verifier = spec.verifier
    if verifier in {"non_empty_text", "terminal_output", "notes_response", "email_result", "calendar_result", "file_result"}:
        if not text:
            return False, f"Verifier '{verifier}' failed: empty output."
        return True, ""
    if verifier == "report_with_sources":
        if len(text) < 40:
            return False, "Verifier 'report_with_sources' failed: report too short."
        return True, ""
    if verifier == "json_object":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return False, "Verifier 'json_object' failed: invalid JSON."
        if not isinstance(payload, dict):
            return False, "Verifier 'json_object' failed: JSON is not an object."
        return True, ""
    if verifier == "json_array_or_object":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return False, "Verifier 'json_array_or_object' failed: invalid JSON."
        if not isinstance(payload, (dict, list)):
            return False, "Verifier 'json_array_or_object' failed: JSON is not object/array."
        return True, ""
    return True, ""


def _trace_step(trace: dict) -> str:
    _ensure_trace_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    file_name = f"{stamp}_{_safe_slug(trace.get('tool', 'step'))}.json"
    path = TRACE_DIR / file_name
    path.write_text(json.dumps(trace, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _http_json(method: str, path: str, payload: dict | None = None) -> tuple[bool, str]:
    base = DEFAULT_MALWARE_API_BASE.rstrip("/")
    url = f"{base}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read().decode("utf-8", errors="replace")
            return True, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return False, f"HTTP {exc.code}: {body}"
    except Exception as exc:
        return False, f"Request failed: {exc}"


def _execute_tool_call(tool: str, params: dict, step: TaskStep, step_results: dict[int, str]) -> tuple[bool, str]:
    if tool == "research":
        from research import deep_research
        query = params.get("query", params.get("topic", step.description))
        depth = int(params.get("depth", 2))
        result = deep_research(query, depth=depth)
        return True, result["report"]

    if tool == "git":
        from tools.git_ops import dispatch as _git_dispatch
        action = params.get("action", "status")
        return _git_dispatch(action, params)

    if tool == "search":
        from harness import web_search as _ws
        query = params.get("query", step.description)
        max_results = int(params.get("max_results", 5))
        fetch_top = str(params.get("fetch_top", "false")).lower() in ("true", "1", "yes")
        if fetch_top:
            return True, _ws.search_and_fetch(query, max_results=max_results)
        return True, _ws.search(query, max_results=max_results)

    if tool == "fetch_page":
        from harness import web_search as _ws
        url = params.get("url", "").strip()
        if not url:
            return False, "fetch_page requires a url parameter"
        max_chars = int(params.get("max_chars", 6000))
        return True, _ws.fetch_page(url, max_chars=max_chars)

    if tool == "notes":
        import notes as notes_mod
        action = (params.get("action", "write") or "write").strip().lower()
        if action in {"read", "list"}:
            return True, notes_mod.get_notes()
        content = params.get("content", params.get("text", step_results.get(max(step_results.keys(), default=0), "")))
        title = params.get("title", "Jarvis Note")
        return True, notes_mod.add_note(f"# {title}\n\n{content}")

    if tool == "file":
        import terminal
        action = (params.get("action", "write") or "write").strip().lower()
        path = params.get("path", "~/Desktop/jarvis_output.md")
        if action == "write":
            content = params.get("content", step_results.get(max(step_results.keys(), default=0), ""))
            return True, terminal.write_file(path, content)
        return True, terminal.read_file(path)

    if tool == "email":
        import google_services as gs
        action = (params.get("action", "read") or "read").strip().lower()
        if action == "read":
            return True, gs.get_unread_emails(max_results=5)
        return (
            False,
            "Email sending requires an explicit router confirmation draft. "
            "Ask Jarvis to draft the email, then confirm send.",
        )

    if tool == "calendar":
        import google_services as gs
        return True, gs.get_todays_events()

    if tool == "terminal":
        import terminal
        cmd = params.get("command", params.get("cmd", "")).strip()
        if not cmd:
            return False, "No command specified."
        return True, terminal.run_command(cmd)

    if tool == "weather":
        from tools import get_weather
        return True, get_weather()

    if tool == "malware_get_alert":
        alert_id = params.get("alert_id", "").strip()
        safe_id = urllib.parse.quote(alert_id, safe="")
        return _http_json("GET", f"/alerts/{safe_id}")

    if tool == "malware_get_case":
        case_id = params.get("case_id", "").strip()
        safe_id = urllib.parse.quote(case_id, safe="")
        return _http_json("GET", f"/cases/{safe_id}")

    if tool == "malware_list_samples":
        status = urllib.parse.quote(params.get("status", "open"), safe="")
        family = params.get("family", "")
        limit = int(params.get("limit", 25))
        query = f"/samples?status={status}&limit={limit}"
        if family:
            safe_family = urllib.parse.quote(family, safe="")
            query += f"&family={safe_family}"
        return _http_json("GET", query)

    if tool == "malware_submit_hash":
        hash_value = params.get("hash", "").strip()
        source = params.get("source", "jarvis")
        if not hash_value:
            return False, "Missing required hash."
        return _http_json("POST", "/ioc/hash", {"hash": hash_value, "source": source})

    if tool == "osint_username":
        import osint_tools
        result = osint_tools.username_lookup(
            params.get("username", ""),
            timeout_seconds=int(params.get("timeout_seconds", 45)),
            top_sites=int(params.get("top_sites", 200)),
            max_results=int(params.get("max_results", 25)),
        )
        return True, json.dumps(result)

    if tool == "osint_domain_typos":
        import osint_tools
        result = osint_tools.domain_typo_scan(
            params.get("domain", ""),
            timeout_seconds=int(params.get("timeout_seconds", 60)),
            max_results=int(params.get("max_results", 25)),
            registered_only=bool(params.get("registered_only", True)),
        )
        return True, json.dumps(result)

    if tool == "osint_subdomains":
        import osint_tools
        result = osint_tools.subdomain_enum(
            params.get("domain", ""),
            timeout_seconds=int(params.get("timeout_seconds", 60)),
            max_results=int(params.get("max_results", 100)),
            passive_only=bool(params.get("passive_only", True)),
        )
        return True, json.dumps(result)

    if tool == "osint_whois":
        import osint_tools
        result = osint_tools.whois_lookup(
            params.get("domain", ""),
            timeout_seconds=int(params.get("timeout_seconds", 15)),
        )
        return True, json.dumps(result)

    if tool == "specialized_agent":
        import agent_dispatch
        agent_name = params.get("agent", "").strip()
        agent_task = params.get("task", step.description)
        context = params.get("context", "")
        try:
            chunks = list(agent_dispatch.dispatch(agent_name, agent_task, context))
            return True, "".join(chunks)
        except RuntimeError as exc:
            return False, str(exc)

    if tool == "code_task":
        import coder_workbench
        agent_task = params.get("task", step.description)
        max_iter = int(params.get("max_iterations", 5))
        result = coder_workbench.fix_loop(agent_task, max_iterations=max_iter)
        if result.get("ok"):
            return True, f"Code task completed in {result['iterations']} iteration(s).\n{result['output']}"
        return False, result.get("error", "Code task failed") + f"\n{result.get('output', '')}"

    prompt = params.get("prompt", params.get("content", step.description))
    if step_results:
        last = step_results.get(max(step_results.keys()))
        if last and "$" not in prompt:
            prompt = f"Context from previous step:\n{last[:1500]}\n\nTask: {prompt}"
    system_extra, _ = skills.build_system_extra(prompt, tool="chat")
    if DEFAULT_MODE != "cloud":
        try:
            import ollama as _ollama_lib
            from brains.brain_ollama import get_best_available
            try:
                import httpx as _httpx
                _local_client = _ollama_lib.Client(
                    timeout=_httpx.Timeout(connect=10.0, read=90.0, write=15.0, pool=10.0)
                )
            except ImportError:
                _local_client = _ollama_lib.Client(timeout=90.0)
            _model = get_best_available(LOCAL_DEFAULT)
            _messages = [{"role": "user", "content": prompt}]
            if system_extra:
                _messages = [{"role": "system", "content": system_extra}] + _messages
            _resp = _local_client.chat(model=_model, messages=_messages, stream=False)
            return True, (_resp.message.content or "").strip()
        except Exception:
            log.debug("[ExecutionEngine] local chat fallback failed; trying cloud", exc_info=True)
    return True, ask_claude(prompt, model=SONNET, system_extra=system_extra)


def execute_step(step: TaskStep, step_results: dict[int, str], run_id: str = "") -> tuple[bool, str]:
    resolved = resolve_params(step.params, step_results)
    tool = (step.tool or "chat").strip().lower()
    spec = tool_registry.get_tool_spec(tool)
    if not spec:
        spec = tool_registry.get_tool_spec("chat")
        tool = "chat"
        resolved = {"prompt": step.description}

    ok_args, normalized, arg_error = tool_registry.validate_args(tool, resolved)
    if not ok_args:
        trace_path = _trace_step(
            {
                "timestamp": _now_iso(),
                "run_id": run_id,
                "step_number": step.number,
                "description": step.description,
                "tool": tool,
                "params": resolved,
                "normalized_params": {},
                "ok": False,
                "error": arg_error,
                "phase": "precheck",
            }
        )
        return False, f"{arg_error} Trace: {trace_path}"

    attempts = 2 if spec.idempotent else 1
    started = time.time()
    last_result = ""
    last_error = ""
    success = False
    attempts_used = 0
    for attempt in range(1, attempts + 1):
        attempts_used = attempt
        call_ok, result = _execute_tool_call(tool, normalized, step, step_results)
        last_result = result
        if not call_ok:
            last_error = result
            continue
        verify_ok, verify_error = _verify_result(spec, result)
        if verify_ok:
            success = True
            last_error = ""
            break
        last_error = verify_error

    elapsed_ms = int((time.time() - started) * 1000)
    trace = {
        "timestamp": _now_iso(),
        "run_id": run_id,
        "step_number": step.number,
        "description": step.description,
        "tool": tool,
        "params": resolved,
        "normalized_params": normalized,
        "ok": success,
        "attempts": attempts_used,
        "elapsed_ms": elapsed_ms,
        "side_effects": spec.side_effects,
        "idempotent": spec.idempotent,
        "verifier": spec.verifier,
        "result_preview": (last_result or "")[:500],
        "error": last_error,
    }
    trace_path = _trace_step(trace)
    if success:
        return True, last_result
    message = last_error or last_result or "Step failed without details."
    return False, f"{message} Trace: {trace_path}"
