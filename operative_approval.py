from __future__ import annotations

import datetime as dt
import getpass
import hashlib
import json
import re
import secrets
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import tool_registry
from execution_engine import required_capabilities_for_tool
from task_planner import TaskStep


_MANIFEST_VERSION = 1
_DYNAMIC_STEP_REFERENCE = re.compile(r"\$step_\d+_result", re.IGNORECASE)
_APPROVAL_ID_PATTERN = re.compile(r"\bop_[A-Za-z0-9_-]{12,80}\b")
_BLOCKED_PRIVILEGED_TOOLS = frozenset({"terminal", "code_task", "specialized_agent"})


class ApprovalError(ValueError):
    """Raised when a plan cannot be represented by a safe approval manifest."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat()


def _parse_time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def new_approval_id() -> str:
    return "op_" + secrets.token_urlsafe(18)


def redact_approval_ids(value: str) -> str:
    return _APPROVAL_ID_PATTERN.sub("op_[REDACTED]", str(value or ""))


def redact_approval_data(value: Any) -> Any:
    """Recursively remove approval bearer IDs from telemetry-bound values."""
    if isinstance(value, Mapping):
        return {str(key): redact_approval_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_approval_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_approval_data(item) for item in value)
    if isinstance(value, str):
        return redact_approval_ids(value)
    return value


@dataclass(frozen=True)
class RouteContext:
    principal: str
    session_id: str
    source: str
    authenticated: bool = True

    @classmethod
    def desktop(cls) -> "RouteContext":
        return cls(
            principal=getpass.getuser() or "local-user",
            session_id="desktop",
            source="desktop",
            authenticated=True,
        )

    def normalized(self) -> "RouteContext":
        principal = str(self.principal or "").strip()
        session_id = str(self.session_id or "").strip()
        source = str(self.source or "").strip()
        if not principal or not session_id or not source:
            raise ApprovalError("Approval context requires principal, session, and source.")
        if any(len(value) > 200 for value in (principal, session_id, source)):
            raise ApprovalError("Approval context fields must be at most 200 characters.")
        return RouteContext(principal, session_id, source, bool(self.authenticated))


@dataclass(frozen=True)
class ExecutionManifest:
    task: str
    plan_json: str
    capabilities: tuple[str, ...]
    resources_json: str
    budget_json: str
    provider_policy_json: str
    principal: str
    session_id: str
    source: str
    created_at: str
    expires_at: str
    version: int = _MANIFEST_VERSION

    @property
    def plan(self) -> list[dict[str, Any]]:
        return json.loads(self.plan_json)

    @property
    def resources(self) -> list[dict[str, Any]]:
        return json.loads(self.resources_json)

    @property
    def budget(self) -> dict[str, Any]:
        return json.loads(self.budget_json)

    @property
    def provider_policy(self) -> dict[str, Any]:
        return json.loads(self.provider_policy_json)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "task": self.task,
            "plan": self.plan,
            "capabilities": list(self.capabilities),
            "resources": self.resources,
            "budget": self.budget,
            "provider_policy": self.provider_policy,
            "principal": self.principal,
            "session_id": self.session_id,
            "source": self.source,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }

    @property
    def digest(self) -> str:
        return canonical_sha256(self.to_dict())

    def is_expired(self, now: dt.datetime | None = None) -> bool:
        return _parse_time(self.expires_at) <= (now or _utc_now())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionManifest":
        if int(value.get("version", 0)) != _MANIFEST_VERSION:
            raise ApprovalError("Unsupported execution manifest version.")
        context = RouteContext(
            principal=str(value.get("principal") or ""),
            session_id=str(value.get("session_id") or ""),
            source=str(value.get("source") or ""),
            authenticated=True,
        ).normalized()
        task = str(value.get("task") or "").strip()
        plan = value.get("plan")
        resources = value.get("resources")
        budget = value.get("budget")
        provider_policy = value.get("provider_policy")
        capabilities = value.get("capabilities")
        if not task or not isinstance(plan, list) or not plan:
            raise ApprovalError("Execution manifest requires a task and non-empty plan.")
        if not isinstance(resources, list) or not isinstance(budget, Mapping):
            raise ApprovalError("Execution manifest resources or budget are invalid.")
        if not isinstance(provider_policy, Mapping) or not isinstance(capabilities, list):
            raise ApprovalError("Execution manifest policy or capabilities are invalid.")
        created_at = str(value.get("created_at") or "")
        expires_at = str(value.get("expires_at") or "")
        _parse_time(created_at)
        _parse_time(expires_at)
        return cls(
            task=task,
            plan_json=_canonical_json(plan),
            capabilities=tuple(sorted({str(item) for item in capabilities if str(item)})),
            resources_json=_canonical_json(resources),
            budget_json=_canonical_json(dict(budget)),
            provider_policy_json=_canonical_json(dict(provider_policy)),
            principal=context.principal,
            session_id=context.session_id,
            source=context.source,
            created_at=created_at,
            expires_at=expires_at,
        )


@dataclass(frozen=True)
class ExecutionGrant:
    approval_id: str
    manifest_digest: str
    principal: str
    session_id: str
    source: str
    run_id: str
    grant_expires_at: str
    capabilities: tuple[str, ...]
    resources_json: str

    @property
    def resources(self) -> list[dict[str, Any]]:
        return json.loads(self.resources_json)

    def to_scope(self) -> dict[str, Any]:
        return {
            "approval_id": self.approval_id,
            "manifest_digest": self.manifest_digest,
            "principal": self.principal,
            "session_id": self.session_id,
            "source": self.source,
            "run_id": self.run_id,
            "grant_expires_at": self.grant_expires_at,
            "capabilities": list(self.capabilities),
            "resources": self.resources,
        }


def tool_call_sha256(tool: str, params: Mapping[str, Any]) -> str:
    return canonical_sha256({"tool": str(tool).strip().lower(), "params": dict(params)})


def _contains_dynamic_reference(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_contains_dynamic_reference(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_dynamic_reference(item) for item in value)
    return bool(_DYNAMIC_STEP_REFERENCE.search(str(value)))


def _resolved_path(value: str) -> str:
    raw = str(value or "").strip()
    if not raw or "\x00" in raw:
        raise ApprovalError("File approval requires a valid path.")
    return str(Path(raw).expanduser().resolve(strict=False))


def _network_origin(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(value))
        port = parsed.port
    except ValueError as exc:
        raise ApprovalError("Network approval contains an invalid URL.") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ApprovalError("Network approval requires an explicit HTTP(S) origin.")
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}:{port or default_port}"


def _resource_for_step(step_number: int, tool: str, params: Mapping[str, Any]) -> dict[str, Any]:
    resource: dict[str, Any] = {
        "step_number": int(step_number),
        "tool": tool,
        "call_sha256": tool_call_sha256(tool, params),
        "kind": "tool_call",
    }
    if tool == "file":
        resource.update(
            kind="file",
            action=str(params.get("action") or ""),
            path=str(params.get("path") or ""),
            content_sha256=canonical_sha256(str(params.get("content") or "")),
        )
    elif tool == "email":
        if str(params.get("action") or "").lower() == "send":
            resource.update(
                kind="email_send",
                recipient=str(params.get("to") or "").strip().lower(),
                subject=str(params.get("subject") or ""),
                subject_sha256=canonical_sha256(str(params.get("subject") or "")),
                body_sha256=canonical_sha256(str(params.get("body") or "")),
            )
        else:
            resource.update(kind="email_read", mailbox="primary")
    elif tool == "notes":
        resource.update(
            kind="notes",
            action=str(params.get("action") or ""),
            collection="jarvis-notes",
            title=str(params.get("title") or ""),
            content_sha256=canonical_sha256(
                str(params.get("content") or params.get("text") or "")
            ),
        )
    elif tool == "calendar":
        resource.update(kind="calendar", calendar="primary")
    elif tool == "fetch_page":
        resource.update(
            kind="network",
            origin=_network_origin(str(params.get("url") or "")),
            url=str(params.get("url") or ""),
        )
    elif tool in {"search", "research", "weather", "osint_username", "osint_domain_typos", "osint_subdomains", "osint_whois"}:
        target = next(
            (
                str(params[key])
                for key in ("query", "topic", "location", "username", "domain")
                if params.get(key)
            ),
            "",
        )
        resource.update(
            kind="network_query",
            target=target,
            query_sha256=canonical_sha256(dict(params)),
        )
    elif tool == "git":
        resource.update(
            kind="git",
            action=str(params.get("action") or ""),
            paths=str(params.get("paths") or params.get("path") or ""),
            repository=str(Path.cwd().resolve()),
        )
    elif tool == "malware_submit_hash":
        resource.update(kind="malware_hash", hash=str(params.get("hash") or "").lower())
    else:
        resource.update(params_sha256=canonical_sha256(dict(params)))
    return resource


def build_manifest(
    task: str,
    steps: Iterable[TaskStep],
    *,
    context: RouteContext,
    budget: Mapping[str, Any],
    provider_policy: Mapping[str, Any],
    approval_ttl_seconds: int,
    now: dt.datetime | None = None,
) -> ExecutionManifest:
    normalized_context = context.normalized()
    if not normalized_context.authenticated:
        raise ApprovalError("Operative approvals require an authenticated route context.")
    task_text = str(task or "").strip()
    if not task_text:
        raise ApprovalError("Task text is required.")
    created = now or _utc_now()
    expires = created + dt.timedelta(seconds=max(1, int(approval_ttl_seconds)))
    plan: list[dict[str, Any]] = []
    step_numbers: set[int] = set()
    capabilities: set[str] = set()
    resources: list[dict[str, Any]] = []
    for index, original in enumerate(steps, start=1):
        tool = str(original.tool or "chat").strip().lower() or "chat"
        ok, normalized, error = tool_registry.validate_args(tool, original.params or {})
        if not ok:
            raise ApprovalError(f"Step {index} is not executable: {error}")
        if tool == "file":
            normalized["path"] = _resolved_path(str(normalized.get("path") or ""))
        required = required_capabilities_for_tool(tool, normalized)
        if "unclassified_side_effect" in required:
            raise ApprovalError(f"Step {index} uses an unclassified side effect.")
        if required and tool in _BLOCKED_PRIVILEGED_TOOLS:
            raise ApprovalError(f"Tool '{tool}' requires an isolated execution contract.")
        if required and tool == "git":
            raise ApprovalError("Git write operations require a repository-state approval contract.")
        if (
            tool == "notes"
            and str(normalized.get("action") or "").lower() == "write"
            and not str(normalized.get("content") or normalized.get("text") or "").strip()
        ):
            raise ApprovalError("Notes writes require explicit approved content.")
        if required and _contains_dynamic_reference(normalized):
            raise ApprovalError(
                f"Step {index} has a dynamic privileged argument and requires a separate approval."
            )
        capabilities.update(required)
        step_number = int(original.number or index)
        if step_number <= 0 or step_number in step_numbers:
            raise ApprovalError("Plan step numbers must be unique positive integers.")
        step_numbers.add(step_number)
        plan.append(
            {
                "number": step_number,
                "description": str(original.description or "").strip(),
                "tool": tool,
                "params": normalized,
            }
        )
        if required:
            resources.append(_resource_for_step(step_number, tool, normalized))
    if not plan:
        raise ApprovalError("Planner returned an empty task plan.")
    return ExecutionManifest(
        task=task_text,
        plan_json=_canonical_json(plan),
        capabilities=tuple(sorted(capabilities)),
        resources_json=_canonical_json(resources),
        budget_json=_canonical_json(dict(budget)),
        provider_policy_json=_canonical_json(dict(provider_policy)),
        principal=normalized_context.principal,
        session_id=normalized_context.session_id,
        source=normalized_context.source,
        created_at=_iso(created),
        expires_at=_iso(expires),
    )


def manifest_steps(manifest: ExecutionManifest) -> list[TaskStep]:
    return [
        TaskStep(
            number=int(item["number"]),
            description=str(item.get("description") or ""),
            tool=str(item.get("tool") or "chat"),
            params=dict(item.get("params") or {}),
        )
        for item in manifest.plan
    ]


def validate_manifest_semantics(manifest: ExecutionManifest) -> bool:
    """Re-derive security fields from the stored task plan before approval."""
    created = _parse_time(manifest.created_at)
    expires = _parse_time(manifest.expires_at)
    ttl_seconds = max(1, int((expires - created).total_seconds()))
    try:
        rebuilt = build_manifest(
            manifest.task,
            manifest_steps(manifest),
            context=RouteContext(
                manifest.principal,
                manifest.session_id,
                manifest.source,
                True,
            ),
            budget=manifest.budget,
            provider_policy=manifest.provider_policy,
            approval_ttl_seconds=ttl_seconds,
            now=created,
        )
    except ApprovalError:
        return False
    return rebuilt.to_dict() == manifest.to_dict()


def approval_summary(manifest: ExecutionManifest, approval_id: str) -> str:
    resources = manifest.resources
    descriptions: list[str] = []
    for item in resources:
        kind = item["kind"]
        if kind == "file":
            detail = (
                f"{item.get('action')} {item.get('path')} "
                f"content sha256 {item.get('content_sha256')}"
            )
        elif kind == "email_send":
            detail = (
                f"email {item.get('recipient')} subject {item.get('subject')!r} "
                f"body sha256 {item.get('body_sha256')}"
            )
        elif kind == "email_read":
            detail = f"read mailbox {item.get('mailbox')}"
        elif kind == "network":
            detail = f"access {item.get('url')}"
        elif kind == "network_query":
            detail = f"query network for {item.get('target')!r}"
        elif kind == "git":
            detail = f"git {item.get('action')} in {item.get('repository')}"
        elif kind == "notes":
            detail = (
                f"notes {item.get('action')} {item.get('title')!r} "
                f"content sha256 {item.get('content_sha256')}"
            )
        else:
            detail = kind
        descriptions.append(f"step {item['step_number']}: {detail}")
    actions = "; ".join(descriptions) or "no privileged actions"
    return (
        f"Approval required for {len(resources)} privileged action(s) "
        f"({', '.join(manifest.capabilities)}): {actions}. "
        f"Run /task approve {approval_id} before {manifest.expires_at}."
    )
