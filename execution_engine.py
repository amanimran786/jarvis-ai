from __future__ import annotations

import json
import http.client
import ipaddress
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Iterable, Iterator

log = logging.getLogger("jarvis.execution_engine")
from pathlib import Path

from config import DEFAULT_MODE, LOCAL_DEFAULT
from provider_priority import ask_with_priority
import skills
import tool_registry
import runtime_state
from task_planner import TaskStep


TRACE_DIR = runtime_state.writable_data_path("training", "execution_traces")
DEFAULT_MALWARE_API_BASE = os.getenv("JARVIS_MALWARE_API_BASE", "http://127.0.0.1:9100").strip()

CAP_LOCAL_WRITE = "local_write"
CAP_LOCAL_READ = "local_read"
CAP_SHELL_EXECUTE = "shell_execute"
CAP_GIT_WRITE = "git_write"
CAP_AGENT_DELEGATE = "agent_delegate"
CAP_MALWARE_SUBMIT = "malware_submit"
CAP_EMAIL_SEND = "email_send"
CAP_NETWORK_ACCESS = "network_access"
CAP_PERSONAL_DATA_READ = "personal_data_read"
CAP_UNRESTRICTED_SHELL = "unrestricted_shell"

_KNOWN_CAPABILITIES = frozenset({
    CAP_LOCAL_WRITE,
    CAP_LOCAL_READ,
    CAP_SHELL_EXECUTE,
    CAP_GIT_WRITE,
    CAP_AGENT_DELEGATE,
    CAP_MALWARE_SUBMIT,
    CAP_EMAIL_SEND,
    CAP_NETWORK_ACCESS,
    CAP_PERSONAL_DATA_READ,
    CAP_UNRESTRICTED_SHELL,
})
_ALLOW_DIRECTIVE = re.compile(r"^\s*\[allow:([a-z0-9_,\s-]+)\]\s*", re.IGNORECASE)

_ACTIVE_CAPABILITIES: ContextVar[frozenset[str]] = ContextVar(
    "jarvis_execution_capabilities",
    default=frozenset(),
)
_EXECUTION_DEADLINE: ContextVar[float | None] = ContextVar(
    "jarvis_execution_deadline",
    default=None,
)
_SENSITIVE_STEP_RESULTS: ContextVar[frozenset[int]] = ContextVar(
    "jarvis_sensitive_step_results",
    default=frozenset(),
)
_UNAVAILABLE_STEP_RESULTS: ContextVar[frozenset[int]] = ContextVar(
    "jarvis_unavailable_step_results",
    default=frozenset(),
)


def capabilities_for_task(
    task: str,
    *,
    trusted_capabilities: Iterable[str] | None = None,
) -> frozenset[str]:
    """Normalize a trusted caller grant; task text never grants capabilities."""
    del task
    requested = {
        str(value).strip().lower().replace("-", "_")
        for value in (trusted_capabilities or ())
        if str(value).strip()
    }
    return frozenset(requested & _KNOWN_CAPABILITIES)


def task_without_capability_directive(task: str) -> str:
    """Remove the trusted grant prefix before sending task text to a model."""
    return _ALLOW_DIRECTIVE.sub("", task or "", count=1).strip()


@contextmanager
def execution_capability_scope(
    capabilities: Iterable[str],
    *,
    deadline: float | None = None,
    sensitive_step_numbers: Iterable[int] = (),
    unavailable_step_numbers: Iterable[int] = (),
) -> Iterator[frozenset[str]]:
    """Apply an immutable capability grant to execution in the current context."""
    normalized = frozenset(str(item).strip() for item in capabilities if str(item).strip())
    capability_token = _ACTIVE_CAPABILITIES.set(normalized)
    deadline_token = _EXECUTION_DEADLINE.set(deadline)
    sensitive_token = _SENSITIVE_STEP_RESULTS.set(
        frozenset(int(number) for number in sensitive_step_numbers)
    )
    unavailable_token = _UNAVAILABLE_STEP_RESULTS.set(
        frozenset(int(number) for number in unavailable_step_numbers)
    )
    try:
        yield normalized
    finally:
        _UNAVAILABLE_STEP_RESULTS.reset(unavailable_token)
        _SENSITIVE_STEP_RESULTS.reset(sensitive_token)
        _EXECUTION_DEADLINE.reset(deadline_token)
        _ACTIVE_CAPABILITIES.reset(capability_token)


def sensitive_step_numbers() -> frozenset[int]:
    """Return the tainted step numbers in the current execution context."""
    return _SENSITIVE_STEP_RESULTS.get()


def required_capabilities_for_tool(tool: str, params: dict) -> frozenset[str]:
    """Return action-aware capabilities required before a normalized tool call."""
    normalized_tool = (tool or "").strip().lower()
    action = str(params.get("action", "")).strip().lower()
    if normalized_tool == "git":
        if action in {"status", "diff", "log", "branch", "show"}:
            return frozenset()
        return frozenset({CAP_GIT_WRITE})
    if normalized_tool == "file":
        return frozenset({CAP_LOCAL_READ}) if action == "read" else frozenset({CAP_LOCAL_WRITE})
    if normalized_tool == "notes":
        if action in {"read", "list"}:
            return frozenset({CAP_PERSONAL_DATA_READ})
        return frozenset({CAP_LOCAL_WRITE})
    if normalized_tool == "email":
        return (
            frozenset({CAP_PERSONAL_DATA_READ})
            if action == "read"
            else frozenset({CAP_EMAIL_SEND})
        )
    if normalized_tool == "calendar":
        return frozenset({CAP_PERSONAL_DATA_READ})
    if normalized_tool == "terminal":
        return frozenset({CAP_SHELL_EXECUTE, CAP_UNRESTRICTED_SHELL})
    if normalized_tool == "malware_submit_hash":
        return frozenset({CAP_MALWARE_SUBMIT})
    if normalized_tool == "specialized_agent":
        return frozenset({
            CAP_AGENT_DELEGATE,
            CAP_LOCAL_READ,
            CAP_LOCAL_WRITE,
            CAP_NETWORK_ACCESS,
            CAP_SHELL_EXECUTE,
            CAP_UNRESTRICTED_SHELL,
        })
    if normalized_tool == "code_task":
        return frozenset({
            CAP_LOCAL_READ,
            CAP_LOCAL_WRITE,
            CAP_SHELL_EXECUTE,
            CAP_UNRESTRICTED_SHELL,
        })
    if normalized_tool in {
        "search",
        "fetch_page",
        "research",
        "weather",
        "osint_username",
        "osint_domain_typos",
        "osint_subdomains",
        "osint_whois",
    }:
        return frozenset({CAP_NETWORK_ACCESS})
    spec = tool_registry.get_tool_spec(normalized_tool)
    if spec and spec.side_effects:
        return frozenset({"unclassified_side_effect"})
    return frozenset()


def _referenced_step_numbers(params: dict) -> frozenset[int]:
    references: set[int] = set()
    for value in params.values():
        if not isinstance(value, str):
            continue
        match = re.fullmatch(r"\$step_(\d+)_result", value.strip())
        if match:
            references.add(int(match.group(1)))
    return frozenset(references)


def _is_sensitive_result(tool: str, params: dict) -> bool:
    action = str(params.get("action", "")).strip().lower()
    if tool == "file":
        return action == "read"
    if tool in {"notes", "email"}:
        return action in {"read", "list"}
    if tool == "git":
        return action in {"status", "diff", "log", "branch", "show"}
    return tool in {
        "calendar",
        "malware_get_alert",
        "malware_get_case",
        "malware_list_samples",
    }


def _is_outbound_tool(tool: str) -> bool:
    return tool in {
        "search",
        "fetch_page",
        "research",
        "weather",
        "osint_username",
        "osint_domain_typos",
        "osint_subdomains",
        "osint_whois",
        "specialized_agent",
        "malware_submit_hash",
    }


def _resolve_public_http_url(url: str) -> tuple[object | None, tuple[str, ...], str]:
    """Resolve one public HTTP URL and return its validated address set."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None, (), "fetch_page requires an http(s) URL with a hostname"
    if parsed.username or parsed.password:
        return None, (), "fetch_page does not accept credentials in URLs"
    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".local"):
        return None, (), "fetch_page blocks local destinations"
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        return None, (), f"fetch_page rejected invalid port: {exc}"
    try:
        literal_ip = ipaddress.ip_address(hostname)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and not literal_ip.is_global:
        return None, (), "fetch_page blocks private, loopback, link-local, and reserved destinations"
    try:
        addresses = {str(literal_ip)} if literal_ip is not None else {
            info[4][0]
            for info in socket.getaddrinfo(
                hostname,
                port,
                type=socket.SOCK_STREAM,
            )
        }
    except (OSError, ValueError) as exc:
        return None, (), f"fetch_page could not resolve destination: {exc}"
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return None, (), "fetch_page blocks private, loopback, link-local, and reserved destinations"
    if not addresses:
        return None, (), "fetch_page destination resolved to no addresses"
    return parsed, tuple(sorted(addresses)), ""


def _validate_public_http_url(url: str) -> str:
    """Return an error for non-HTTP or non-public destinations, else empty text."""
    _, _, error = _resolve_public_http_url(url)
    return error


def _pinned_http_request(
    parsed,
    address: str,
    *,
    timeout: float,
    max_bytes: int,
) -> tuple[int, dict[str, str], bytes]:
    """Connect to the validated IP while preserving Host and TLS verification."""
    hostname = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    connection_type = (
        http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    )
    connection = connection_type(hostname, port=port, timeout=timeout)

    def _create_connection(_target, connect_timeout=None, source_address=None, **_kwargs):
        return socket.create_connection(
            (address, port),
            timeout=connect_timeout if connect_timeout is not None else timeout,
            source_address=source_address,
        )

    connection._create_connection = _create_connection
    target = urllib.parse.urlunparse(("", "", parsed.path or "/", "", parsed.query, ""))
    try:
        connection.request(
            "GET",
            target,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Jarvis/1.0)"},
        )
        response = connection.getresponse()
        headers = {name.lower(): value for name, value in response.getheaders()}
        return response.status, headers, response.read(max_bytes)
    finally:
        connection.close()


def _fetch_public_page(url: str, max_chars: int) -> tuple[bool, str]:
    """Fetch a public page while validating each redirect destination."""
    from harness import web_search as _ws

    current_url = url
    fetch_deadline = time.monotonic() + 20.0
    execution_deadline = _EXECUTION_DEADLINE.get()
    if execution_deadline is not None:
        fetch_deadline = min(fetch_deadline, execution_deadline)
    max_bytes = min(max(max_chars * 4, 16_384), 1_000_000)
    for _ in range(6):
        parsed, addresses, url_error = _resolve_public_http_url(current_url)
        if url_error:
            return False, url_error
        last_error = ""
        for address in addresses:
            remaining = fetch_deadline - time.monotonic()
            if remaining <= 0:
                return False, "Could not fetch page: execution deadline exceeded"
            try:
                status, headers, raw = _pinned_http_request(
                    parsed,
                    address,
                    timeout=max(0.1, remaining),
                    max_bytes=max_bytes,
                )
                break
            except Exception as exc:
                last_error = str(exc)
        else:
            return False, f"Could not fetch page: {last_error or 'connection failed'}"
        if status in {301, 302, 303, 307, 308}:
            location = headers.get("location", "")
            if not location:
                return False, "Could not fetch page: redirect missing Location header"
            current_url = urllib.parse.urljoin(current_url, location)
            continue
        if status >= 400:
            return False, f"Could not fetch page: HTTP {status}"
        return True, _ws._strip_html(raw, max_chars)
    return False, "Could not fetch page: redirect limit exceeded"


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
            match = re.fullmatch(r"\$step_(\d+)_result", value.strip())
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
    redacted = dict(trace)
    for key in ("params", "normalized_params"):
        values = redacted.get(key)
        if isinstance(values, dict):
            redacted[key] = {name: "[REDACTED]" for name in values}
    if "result_preview" in redacted:
        redacted["result_preview"] = "[REDACTED]"
    if redacted.get("description"):
        redacted["description"] = "[REDACTED]"
    if redacted.get("error"):
        redacted["error"] = "[REDACTED]"
    path.write_text(json.dumps(redacted, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _tool_result_indicates_failure(tool: str, result: str) -> bool:
    if tool not in {"terminal", "file", "notes", "git"}:
        return False
    lowered = (result or "").strip().lower()
    prefixes = (
        "blocked:",
        "could not ",
        "denied:",
        "directory not found:",
        "error ",
        "error:",
        "file not found:",
        "permission denied",
    )
    if lowered.startswith(prefixes) or " timed out" in lowered:
        return True
    if tool == "git":
        return (
            lowered.startswith(("commit message ", "invalid ref:", "no paths provided", "rejected:"))
            or lowered.startswith("git ") and " failed:" in lowered
        )
    return False


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
        return (
            False,
            "Deep research is disabled for autonomous execution until its fetch path "
            "uses the validated public-network transport.",
        )

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
            return False, "search.fetch_top is disabled; use fetch_page for a validated URL."
        return True, _ws.search(query, max_results=max_results)

    if tool == "fetch_page":
        url = params.get("url", "").strip()
        if not url:
            return False, "fetch_page requires a url parameter"
        max_chars = int(params.get("max_chars", 6000))
        return _fetch_public_page(url, max_chars)

    if tool == "notes":
        import notes as notes_mod
        action = (params.get("action", "write") or "write").strip().lower()
        if action in {"read", "list"}:
            return True, notes_mod.get_notes()
        content = (
            params.get("content")
            or params.get("text")
            or step_results.get(max(step_results.keys(), default=0), "")
        )
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
        return (
            False,
            "Generic terminal execution is disabled for autonomous tasks; use the "
            "bounded code workbench or a confirmed interactive terminal action.",
        )

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
        return (
            False,
            "Specialist delegation is disabled for autonomous tasks until child agents "
            "inherit the parent deadline and capability scope.",
        )

    if tool == "code_task":
        import coder_workbench
        agent_task = params.get("task", step.description)
        max_iter = int(params.get("max_iterations", 5))
        result = coder_workbench.fix_loop(agent_task, max_iterations=max_iter)
        if result.get("ok"):
            return True, f"Code task completed in {result['iterations']} iteration(s).\n{result['output']}"
        return False, result.get("error", "Code task failed") + f"\n{result.get('output', '')}"

    prompt = params.get("prompt")
    if not str(prompt or "").strip():
        prompt = params.get("content")
    if not str(prompt or "").strip():
        prompt = step.description
    latest_step = max(step_results.keys(), default=0)
    sensitive_steps = _SENSITIVE_STEP_RESULTS.get()
    sensitive_context = bool(
        _referenced_step_numbers(step.params or {}) & sensitive_steps
    ) or latest_step in sensitive_steps
    if step_results:
        last = step_results.get(latest_step)
        if last and not _referenced_step_numbers(step.params or {}):
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
            if sensitive_context:
                log.warning(
                    "[ExecutionEngine] local chat failed; sensitive context blocks cloud fallback"
                )
                return False, "Sensitive local data cannot be sent to a cloud model fallback."
            log.debug("[ExecutionEngine] local chat fallback failed; trying cloud", exc_info=True)
    return True, ask_with_priority(prompt, tier="strong", system_extra=system_extra)


def execute_step(step: TaskStep, step_results: dict[int, str], run_id: str = "") -> tuple[bool, str]:
    referenced_steps = _referenced_step_numbers(step.params or {})
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

    required_capabilities = required_capabilities_for_tool(tool, normalized)
    active_capabilities = _ACTIVE_CAPABILITIES.get()
    missing_capabilities = required_capabilities - active_capabilities
    if missing_capabilities:
        authorization_error = (
            "Execution authorization required for capabilities: "
            + ", ".join(sorted(missing_capabilities))
        )
        trace_path = _trace_step(
            {
                "timestamp": _now_iso(),
                "run_id": run_id,
                "step_number": step.number,
                "description": step.description,
                "tool": tool,
                "params": resolved,
                "normalized_params": normalized,
                "ok": False,
                "error": authorization_error,
                "phase": "authorization",
                "required_capabilities": sorted(required_capabilities),
                "active_capabilities": sorted(active_capabilities),
            }
        )
        return False, f"{authorization_error}. Trace: {trace_path}"

    sensitive_steps = _SENSITIVE_STEP_RESULTS.get()
    unavailable_steps = _UNAVAILABLE_STEP_RESULTS.get()
    latest_step = max(step_results.keys(), default=0)
    uses_implicit_sensitive_context = (
        tool == "chat"
        and latest_step in sensitive_steps
        and not referenced_steps
    )
    uses_sensitive_data = bool(referenced_steps & sensitive_steps) or uses_implicit_sensitive_context
    uses_unavailable_data = bool(referenced_steps & unavailable_steps) or (
        tool == "chat"
        and latest_step in unavailable_steps
        and not referenced_steps
    ) or (
        tool == "notes"
        and str(normalized.get("action", "write")).lower() == "write"
        and latest_step in unavailable_steps
        and not str(normalized.get("content") or normalized.get("text") or "").strip()
    )
    if uses_unavailable_data:
        unavailable_error = (
            "Sensitive step output is unavailable after resume; automatic continuation is blocked."
        )
        trace_path = _trace_step(
            {
                "timestamp": _now_iso(),
                "run_id": run_id,
                "step_number": step.number,
                "description": step.description,
                "tool": tool,
                "params": resolved,
                "normalized_params": normalized,
                "ok": False,
                "error": unavailable_error,
                "phase": "resume_data",
                "unavailable_step_numbers": sorted(unavailable_steps),
                "referenced_step_numbers": sorted(referenced_steps),
            }
        )
        return False, f"{unavailable_error} Trace: {trace_path}"
    if (
        (_is_outbound_tool(tool) and referenced_steps & sensitive_steps)
        or (tool == "chat" and DEFAULT_MODE == "cloud" and (
            referenced_steps & sensitive_steps or uses_implicit_sensitive_context
        ))
    ):
        data_flow_error = "Sensitive local data cannot be passed to an outbound tool."
        trace_path = _trace_step(
            {
                "timestamp": _now_iso(),
                "run_id": run_id,
                "step_number": step.number,
                "description": step.description,
                "tool": tool,
                "params": resolved,
                "normalized_params": normalized,
                "ok": False,
                "error": data_flow_error,
                "phase": "data_flow",
                "sensitive_step_numbers": sorted(sensitive_steps),
                "referenced_step_numbers": sorted(referenced_steps),
            }
        )
        return False, f"{data_flow_error} Trace: {trace_path}"

    if tool == "fetch_page":
        url_error = _validate_public_http_url(str(normalized.get("url", "")))
        if url_error:
            trace_path = _trace_step(
                {
                    "timestamp": _now_iso(),
                    "run_id": run_id,
                    "step_number": step.number,
                    "description": step.description,
                    "tool": tool,
                    "params": resolved,
                    "normalized_params": normalized,
                    "ok": False,
                    "error": url_error,
                    "phase": "network_precheck",
                }
            )
            return False, f"{url_error}. Trace: {trace_path}"

    deadline = _EXECUTION_DEADLINE.get()
    if deadline is not None:
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds < spec.timeout_seconds:
            budget_error = (
                f"Insufficient execution budget for '{tool}': "
                f"{max(0.0, remaining_seconds):.1f}s remaining, "
                f"{spec.timeout_seconds}s required."
            )
            trace_path = _trace_step(
                {
                    "timestamp": _now_iso(),
                    "run_id": run_id,
                    "step_number": step.number,
                    "description": step.description,
                    "tool": tool,
                    "params": resolved,
                    "normalized_params": normalized,
                    "ok": False,
                    "error": budget_error,
                    "phase": "budget",
                    "remaining_seconds": max(0.0, remaining_seconds),
                    "required_seconds": spec.timeout_seconds,
                }
            )
            return False, f"{budget_error} Trace: {trace_path}"

    attempts = 2 if spec.idempotent else 1
    started = time.time()
    last_result = ""
    last_error = ""
    success = False
    attempts_used = 0
    for attempt in range(1, attempts + 1):
        if attempt > 1 and deadline is not None:
            remaining_seconds = deadline - time.monotonic()
            if remaining_seconds < spec.timeout_seconds:
                last_error = (
                    f"Retry blocked by execution budget: {max(0.0, remaining_seconds):.1f}s "
                    f"remaining, {spec.timeout_seconds}s required."
                )
                break
        attempts_used = attempt
        call_ok, result = _execute_tool_call(tool, normalized, step, step_results)
        last_result = result
        if call_ok and _tool_result_indicates_failure(tool, result):
            call_ok = False
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
    if _is_sensitive_result(tool, normalized) or uses_sensitive_data:
        _SENSITIVE_STEP_RESULTS.set(sensitive_steps | {step.number})
    if success:
        return True, last_result
    message = last_error or last_result or "Step failed without details."
    return False, f"{message} Trace: {trace_path}"
