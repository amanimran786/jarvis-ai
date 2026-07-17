from __future__ import annotations

from dataclasses import dataclass
from typing import Any

TOOLS = {
    "chat": "General conversation, questions, explanations, advice, opinions.",
    "search": "Web search for current news, facts, prices, recent events.",
    "knowledge": "Search or build Jarvis's local markdown vault and inspect indexed knowledge files.",
    "local_model": "Improve Jarvis's local-model stack by distillation, evaluation, and automation.",
    "skill": "Create or promote reusable Jarvis skills.",
    "deep_research": "Multi-step research producing a cited report.",
    "operative": "Autonomous multi-step task execution.",
    "specialized_agent": "Run scoped specialist-agent passes with planner/executor/reviewer roles.",
    "calendar": "Google Calendar actions.",
    "email": "Gmail actions.",
    "weather": "Current weather.",
    "timer": "Set a countdown timer or reminder.",
    "system": "macOS system control.",
    "app": "Launch a macOS application.",
    "browser": "Control browser tabs and summarize live pages.",
    "terminal": "Run shell commands and file operations.",
    "admin": "Run shell commands with administrator privileges.",
    "notes": "Take, read, and search personal notes.",
    "camera": "Webcam capture or screenshot analysis.",
    "memory": "Save or recall personal facts.",
    "hardware": "Control physical hardware devices.",
    "self_improve": "Modify Jarvis source code when explicitly requested.",
    "meeting": "Smart Listen meeting mode.",
    "message": "Send iMessage/SMS via Messages app.",
    "osint_username": "Local username footprint scan using Maigret.",
    "osint_domain_typos": "Local typo-squatting scan for a domain using DNSTwist.",
    "osint_subdomains": "Passive subdomain enumeration via subfinder.",
    "osint_whois": "WHOIS lookup for domain registration and ownership information.",
    "artifact": "Generate a shareable Local Artifact — a self-contained interactive HTML page (diagrams, dashboards, code walkthroughs, PR reviews, data visualizations, or any team-shareable output) created on-device and saved to the Desktop.",
    "code_task": "Write Python code files and tests locally, run tests, and fix failures in a loop until passing. Use for: implement X, write a function that does Y, fix the failing test, create a script that does Z.",
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_schema: dict[str, dict[str, Any]]
    required: tuple[str, ...]
    side_effects: bool
    timeout_seconds: int
    verifier: str
    idempotent: bool = True
    require_one_of: tuple[tuple[str, ...], ...] = ()


TOOL_SPECS: dict[str, ToolSpec] = {
    "git": ToolSpec(
        name="git",
        description="Run git operations: status, diff, log, branch, show (read-only); add, commit (write). Push is excluded — must be done manually.",
        args_schema={
            "action": {
                "type": "string",
                "choices": ("status", "diff", "log", "branch", "show", "add", "add_all", "commit"),
            },
            "message": {"type": "string", "default": "", "max_length": 500},
            "paths": {"type": "string", "default": "", "max_length": 4096},
            "path": {"type": "string", "default": "", "max_length": 4096},
            "staged": {"type": "bool", "default": False},
            "n": {"type": "int", "default": 10, "min": 1, "max": 50},
            "ref": {"type": "string", "default": "HEAD", "max_length": 200},
            "oneline": {"type": "bool", "default": True},
        },
        required=("action",),
        side_effects=True,
        timeout_seconds=20,
        verifier="non_empty_text",
        idempotent=False,
    ),
    "search": ToolSpec(
        name="search",
        description="Search the web via DuckDuckGo. Returns ranked results with titles, URLs, and snippets, summarised by a local model.",
        args_schema={
            "query": {"type": "string", "max_length": 20_000},
            "max_results": {"type": "int", "default": 5, "min": 1, "max": 20},
            "fetch_top": {"type": "bool", "default": False},
        },
        required=("query",),
        side_effects=False,
        timeout_seconds=30,
        verifier="non_empty_text",
    ),
    "fetch_page": ToolSpec(
        name="fetch_page",
        description="Fetch a web page and return its plain-text content for further processing.",
        args_schema={
            "url": {"type": "string", "max_length": 8192},
            "max_chars": {"type": "int", "default": 6000, "min": 1, "max": 100_000},
        },
        required=("url",),
        side_effects=False,
        timeout_seconds=20,
        verifier="non_empty_text",
    ),
    "research": ToolSpec(
        name="research",
        description="Deep web research with source-backed report.",
        args_schema={
            "query": {"type": "string", "max_length": 20_000},
            "topic": {"type": "string", "max_length": 20_000},
            "depth": {"type": "int", "default": 2, "min": 1, "max": 5},
        },
        required=(),
        side_effects=False,
        timeout_seconds=180,
        verifier="report_with_sources",
        require_one_of=(("query", "topic"),),
    ),
    "notes": ToolSpec(
        name="notes",
        description="Write or read personal notes.",
        args_schema={
            "action": {"type": "string", "default": "write", "choices": ("read", "list", "write")},
            "title": {"type": "string", "default": "Jarvis Note", "max_length": 500},
            "content": {"type": "string", "default": "", "max_length": 1_000_000},
            "text": {"type": "string", "max_length": 1_000_000},
        },
        required=("action",),
        side_effects=True,
        timeout_seconds=15,
        verifier="notes_response",
    ),
    "email": ToolSpec(
        name="email",
        description="Read or send email.",
        args_schema={
            "action": {"type": "string", "default": "read", "choices": ("read", "send")},
            "to": {"type": "string", "default": "", "max_length": 500},
            "subject": {"type": "string", "default": "Jarvis Report", "max_length": 500},
            "body": {"type": "string", "default": "", "max_length": 1_000_000},
        },
        required=("action",),
        side_effects=True,
        timeout_seconds=30,
        verifier="email_result",
        idempotent=False,
    ),
    "calendar": ToolSpec(
        name="calendar",
        description="Read calendar events.",
        args_schema={"action": {"type": "string", "default": "read", "choices": ("read",)}},
        required=("action",),
        side_effects=False,
        timeout_seconds=20,
        verifier="calendar_result",
    ),
    "terminal": ToolSpec(
        name="terminal",
        description="Run a shell command.",
        args_schema={
            "command": {"type": "string", "max_length": 4096},
            "cmd": {"type": "string", "max_length": 4096},
        },
        required=(),
        side_effects=True,
        timeout_seconds=45,
        verifier="terminal_output",
        idempotent=False,
        require_one_of=(("command", "cmd"),),
    ),
    "file": ToolSpec(
        name="file",
        description="Read or write a local file.",
        args_schema={
            "action": {"type": "string", "default": "write", "choices": ("read", "write")},
            "path": {"type": "string", "default": "~/Desktop/jarvis_output.md", "max_length": 4096},
            "content": {"type": "string", "default": "", "max_length": 1_000_000},
        },
        required=("action", "path"),
        side_effects=True,
        timeout_seconds=20,
        verifier="file_result",
        idempotent=False,
    ),
    "weather": ToolSpec(
        name="weather",
        description="Get current weather.",
        args_schema={},
        required=(),
        side_effects=False,
        timeout_seconds=10,
        verifier="non_empty_text",
    ),
    "chat": ToolSpec(
        name="chat",
        description="Text generation only.",
        args_schema={
            "prompt": {"type": "string", "max_length": 50_000},
            "content": {"type": "string", "max_length": 50_000},
        },
        required=(),
        side_effects=False,
        timeout_seconds=95,
        verifier="non_empty_text",
    ),
    "malware_get_alert": ToolSpec(
        name="malware_get_alert",
        description="Fetch malware alert by id from malware detection API.",
        args_schema={"alert_id": {"type": "string", "max_length": 500}},
        required=("alert_id",),
        side_effects=False,
        timeout_seconds=20,
        verifier="json_object",
    ),
    "malware_get_case": ToolSpec(
        name="malware_get_case",
        description="Fetch malware investigation case details by case id.",
        args_schema={"case_id": {"type": "string", "max_length": 500}},
        required=("case_id",),
        side_effects=False,
        timeout_seconds=20,
        verifier="json_object",
    ),
    "malware_list_samples": ToolSpec(
        name="malware_list_samples",
        description="List malware samples by status or family filter.",
        args_schema={
            "status": {"type": "string", "default": "open", "max_length": 100},
            "family": {"type": "string", "default": "", "max_length": 200},
            "limit": {"type": "int", "default": 25, "min": 1, "max": 100},
        },
        required=(),
        side_effects=False,
        timeout_seconds=20,
        verifier="json_array_or_object",
    ),
    "malware_submit_hash": ToolSpec(
        name="malware_submit_hash",
        description="Submit IOC hash to malware detection API for enrichment/scan.",
        args_schema={
            "hash": {"type": "string", "max_length": 256},
            "source": {"type": "string", "default": "jarvis", "max_length": 200},
        },
        required=("hash",),
        side_effects=True,
        timeout_seconds=25,
        verifier="json_object",
        idempotent=False,
    ),
    "osint_username": ToolSpec(
        name="osint_username",
        description="Scan username presence across platforms with local Maigret.",
        args_schema={
            "username": {"type": "string", "max_length": 200},
            "timeout_seconds": {"type": "int", "default": 45, "min": 1, "max": 120},
            "top_sites": {"type": "int", "default": 200, "min": 1, "max": 500},
            "max_results": {"type": "int", "default": 25, "min": 1, "max": 100},
        },
        required=("username",),
        side_effects=False,
        timeout_seconds=120,
        verifier="json_object",
    ),
    "osint_domain_typos": ToolSpec(
        name="osint_domain_typos",
        description="Scan typo-squat domains with local DNSTwist.",
        args_schema={
            "domain": {"type": "string", "max_length": 253},
            "timeout_seconds": {"type": "int", "default": 60, "min": 1, "max": 120},
            "max_results": {"type": "int", "default": 25, "min": 1, "max": 100},
            "registered_only": {"type": "bool", "default": True},
        },
        required=("domain",),
        side_effects=False,
        timeout_seconds=120,
        verifier="json_object",
    ),
    "osint_subdomains": ToolSpec(
        name="osint_subdomains",
        description="Passive subdomain enumeration via subfinder.",
        args_schema={
            "domain": {"type": "string", "max_length": 253},
            "timeout_seconds": {"type": "int", "default": 60, "min": 1, "max": 120},
            "max_results": {"type": "int", "default": 100, "min": 1, "max": 500},
            "passive_only": {"type": "bool", "default": True},
        },
        required=("domain",),
        side_effects=False,
        timeout_seconds=120,
        verifier="json_object",
    ),
    "osint_whois": ToolSpec(
        name="osint_whois",
        description="WHOIS lookup for domain registration and ownership information.",
        args_schema={
            "domain": {"type": "string", "max_length": 253},
            "timeout_seconds": {"type": "int", "default": 15, "min": 1, "max": 60},
        },
        required=("domain",),
        side_effects=False,
        timeout_seconds=30,
        verifier="json_object",
    ),
    "specialized_agent": ToolSpec(
        name="specialized_agent",
        description="Run a specialist agent by name for a scoped sub-task.",
        args_schema={
            "agent": {"type": "string", "max_length": 100},
            "task": {"type": "string", "max_length": 50_000},
            "context": {"type": "string", "default": "", "max_length": 100_000},
        },
        required=("agent", "task"),
        side_effects=True,
        timeout_seconds=120,
        verifier="non_empty_text",
        idempotent=False,
    ),
    "code_task": ToolSpec(
        name="code_task",
        description="Write, test, and fix Python code in a local workspace loop.",
        args_schema={
            "task": {"type": "string", "max_length": 50_000},
            "max_iterations": {"type": "int", "default": 2, "min": 1, "max": 2},
        },
        required=("task",),
        side_effects=True,
        timeout_seconds=800,
        verifier="non_empty_text",
        idempotent=False,
    ),
}


def tools() -> dict[str, str]:
    return dict(TOOLS)


def tool_list_text() -> str:
    return "\n".join(f'  "{k}": {v}' for k, v in TOOLS.items())


def get_tool_spec(tool_name: str) -> ToolSpec | None:
    return TOOL_SPECS.get((tool_name or "").strip().lower())


def validate_args(tool_name: str, params: dict) -> tuple[bool, dict, str]:
    spec = get_tool_spec(tool_name)
    if not spec:
        return False, {}, f"Unknown tool: {tool_name}"
    params = dict(params or {})
    unknown = sorted(set(params) - set(spec.args_schema))
    if unknown:
        return False, {}, (
            f"Unknown argument(s) for tool '{tool_name}': {', '.join(unknown)}."
        )
    for group in spec.require_one_of:
        if not any(key in params and str(params[key]).strip() for key in group):
            return False, {}, (
                f"Missing required argument for tool '{tool_name}': "
                f"one of {', '.join(group)}."
            )
    normalized: dict[str, Any] = {}

    for key, meta in spec.args_schema.items():
        if key in params:
            value = params[key]
        elif "default" in meta:
            value = meta["default"]
        else:
            value = None

        if key in spec.required and (value is None or str(value).strip() == ""):
            return False, {}, f"Missing required argument '{key}' for tool '{tool_name}'."

        if value is None:
            continue

        expected = meta.get("type", "string")
        try:
            if expected == "int":
                value = int(value)
            elif expected == "float":
                value = float(value)
            elif expected == "bool":
                if isinstance(value, bool):
                    value = value
                elif isinstance(value, (int, float)):
                    value = bool(value)
                elif isinstance(value, str):
                    lowered = value.strip().lower()
                    if lowered in {"1", "true", "yes", "on"}:
                        value = True
                    elif lowered in {"0", "false", "no", "off"}:
                        value = False
                    else:
                        raise ValueError("invalid boolean")
                else:
                    raise ValueError("invalid boolean")
            else:
                value = str(value)
        except (TypeError, ValueError):
            return False, {}, f"Invalid type for '{key}' in tool '{tool_name}': expected {expected}."

        if expected in {"int", "float"}:
            minimum = meta.get("min")
            maximum = meta.get("max")
            if minimum is not None and value < minimum:
                return False, {}, (
                    f"Argument '{key}' for tool '{tool_name}' must be at least {minimum}."
                )
            if maximum is not None and value > maximum:
                return False, {}, (
                    f"Argument '{key}' for tool '{tool_name}' must be at most {maximum}."
                )
        if expected == "string":
            max_length = meta.get("max_length")
            if max_length is not None and len(value) > max_length:
                return False, {}, (
                    f"Argument '{key}' for tool '{tool_name}' exceeds {max_length} characters."
                )
        choices = meta.get("choices")
        if choices is not None and value not in choices:
            return False, {}, (
                f"Argument '{key}' for tool '{tool_name}' must be one of: "
                f"{', '.join(str(choice) for choice in choices)}."
            )
        normalized[key] = value
    return True, normalized, ""


def callable_tool_summaries() -> str:
    lines = []
    for spec in TOOL_SPECS.values():
        args = ", ".join(
            f"{name}:{meta.get('type', 'string')}{'?' if name not in spec.required else ''}"
            for name, meta in spec.args_schema.items()
        ) or "no args"
        lines.append(
            f'- {spec.name}({args}) | side_effects={str(spec.side_effects).lower()} | '
            f'timeout={spec.timeout_seconds}s | verifier={spec.verifier} | {spec.description}'
        )
    return "\n".join(lines)
