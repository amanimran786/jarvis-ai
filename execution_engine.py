from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from brains.brain_claude import ask_claude
from config import SONNET
import skills
import tool_registry
from task_planner import TaskStep


TRACE_DIR = Path(__file__).resolve().parent / "training" / "execution_traces"
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

    if tool == "search":
        from tools import web_search
        query = params.get("query", step.description)
        max_results = int(params.get("max_results", 5))
        return True, web_search(query, max_results=max_results)

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

    prompt = params.get("prompt", params.get("content", step.description))
    if step_results:
        last = step_results.get(max(step_results.keys()))
        if last and "$" not in prompt:
            prompt = f"Context from previous step:\n{last[:1500]}\n\nTask: {prompt}"
    system_extra, _ = skills.build_system_extra(prompt, tool="chat")
    return True, ask_claude(prompt, model=SONNET, system_extra=system_extra)


def execute_step(step: TaskStep, step_results: dict[int, str]) -> tuple[bool, str]:
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
    for attempt in range(1, attempts + 1):
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
        "step_number": step.number,
        "description": step.description,
        "tool": tool,
        "params": resolved,
        "normalized_params": normalized,
        "ok": success,
        "attempts": attempts,
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
