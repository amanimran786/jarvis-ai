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


TOOL_SPECS: dict[str, ToolSpec] = {
    "search": ToolSpec(
        name="search",
        description="Quick web search for current info.",
        args_schema={"query": {"type": "string"}, "max_results": {"type": "int", "default": 5}},
        required=("query",),
        side_effects=False,
        timeout_seconds=20,
        verifier="non_empty_text",
    ),
    "research": ToolSpec(
        name="research",
        description="Deep web research with source-backed report.",
        args_schema={"query": {"type": "string"}, "depth": {"type": "int", "default": 2}},
        required=("query",),
        side_effects=False,
        timeout_seconds=180,
        verifier="report_with_sources",
    ),
    "notes": ToolSpec(
        name="notes",
        description="Write or read personal notes.",
        args_schema={
            "action": {"type": "string", "default": "write"},
            "title": {"type": "string", "default": "Jarvis Note"},
            "content": {"type": "string", "default": ""},
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
            "action": {"type": "string", "default": "read"},
            "to": {"type": "string", "default": ""},
            "subject": {"type": "string", "default": "Jarvis Report"},
            "body": {"type": "string", "default": ""},
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
        args_schema={"action": {"type": "string", "default": "read"}},
        required=("action",),
        side_effects=False,
        timeout_seconds=20,
        verifier="calendar_result",
    ),
    "terminal": ToolSpec(
        name="terminal",
        description="Run a shell command.",
        args_schema={"command": {"type": "string"}},
        required=("command",),
        side_effects=True,
        timeout_seconds=45,
        verifier="terminal_output",
        idempotent=False,
    ),
    "file": ToolSpec(
        name="file",
        description="Read or write a local file.",
        args_schema={
            "action": {"type": "string", "default": "write"},
            "path": {"type": "string", "default": "~/Desktop/jarvis_output.md"},
            "content": {"type": "string", "default": ""},
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
        args_schema={"prompt": {"type": "string"}},
        required=("prompt",),
        side_effects=False,
        timeout_seconds=45,
        verifier="non_empty_text",
    ),
    "malware_get_alert": ToolSpec(
        name="malware_get_alert",
        description="Fetch malware alert by id from malware detection API.",
        args_schema={"alert_id": {"type": "string"}},
        required=("alert_id",),
        side_effects=False,
        timeout_seconds=20,
        verifier="json_object",
    ),
    "malware_get_case": ToolSpec(
        name="malware_get_case",
        description="Fetch malware investigation case details by case id.",
        args_schema={"case_id": {"type": "string"}},
        required=("case_id",),
        side_effects=False,
        timeout_seconds=20,
        verifier="json_object",
    ),
    "malware_list_samples": ToolSpec(
        name="malware_list_samples",
        description="List malware samples by status or family filter.",
        args_schema={
            "status": {"type": "string", "default": "open"},
            "family": {"type": "string", "default": ""},
            "limit": {"type": "int", "default": 25},
        },
        required=(),
        side_effects=False,
        timeout_seconds=20,
        verifier="json_array_or_object",
    ),
    "malware_submit_hash": ToolSpec(
        name="malware_submit_hash",
        description="Submit IOC hash to malware detection API for enrichment/scan.",
        args_schema={"hash": {"type": "string"}, "source": {"type": "string", "default": "jarvis"}},
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
            "username": {"type": "string"},
            "timeout_seconds": {"type": "int", "default": 45},
            "top_sites": {"type": "int", "default": 200},
            "max_results": {"type": "int", "default": 25},
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
            "domain": {"type": "string"},
            "timeout_seconds": {"type": "int", "default": 60},
            "max_results": {"type": "int", "default": 25},
            "registered_only": {"type": "bool", "default": True},
        },
        required=("domain",),
        side_effects=False,
        timeout_seconds=120,
        verifier="json_object",
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
        normalized[key] = value

    for key, value in params.items():
        if key not in normalized and key not in spec.args_schema:
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
