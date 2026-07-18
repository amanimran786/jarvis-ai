from __future__ import annotations

import datetime as dt
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Iterator, Mapping

import behavior_hooks as hooks


_ACTIVE_EXECUTION_GRANT: ContextVar[dict[str, Any] | None] = ContextVar(
    "jarvis_active_execution_grant",
    default=None,
)
_CLAIMED_RESOURCE_CALLS: ContextVar[set[tuple[str, str, int, str]] | None] = ContextVar(
    "jarvis_claimed_resource_calls",
    default=None,
)


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalized_scoped_params(tool: str, params: Mapping[str, Any]) -> dict[str, Any]:
    normalized = dict(params)
    if tool == "file" and normalized.get("path"):
        normalized["path"] = str(Path(str(normalized["path"])).expanduser().resolve(strict=False))
    return normalized


@contextmanager
def execution_grant_scope(grant: Mapping[str, Any] | None) -> Iterator[dict[str, Any] | None]:
    scoped = dict(grant) if grant is not None else None
    token = _ACTIVE_EXECUTION_GRANT.set(scoped)
    claims_token = _CLAIMED_RESOURCE_CALLS.set(set() if scoped is not None else None)
    try:
        yield scoped
    finally:
        _CLAIMED_RESOURCE_CALLS.reset(claims_token)
        _ACTIVE_EXECUTION_GRANT.reset(token)


def authorize_tool_call(
    tool: str,
    params: Mapping[str, Any],
    *,
    step_number: int,
    run_id: str,
    required_capabilities: set[str] | frozenset[str],
) -> tuple[bool, str]:
    """Require an exact, unexpired resource grant before privileged dispatch."""
    required = frozenset(str(item) for item in required_capabilities)
    if not required:
        return True, ""
    grant = _ACTIVE_EXECUTION_GRANT.get()
    if grant is None:
        return False, "A digest-bound execution grant is required."
    if str(grant.get("run_id") or "") != str(run_id or ""):
        return False, "The execution grant is bound to a different run."
    try:
        expires = dt.datetime.fromisoformat(
            str(grant.get("grant_expires_at") or "").replace("Z", "+00:00")
        )
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return False, "The execution grant expiry is invalid."
    if expires.astimezone(dt.timezone.utc) <= dt.datetime.now(dt.timezone.utc):
        return False, "The execution grant has expired."
    granted_capabilities = frozenset(str(item) for item in grant.get("capabilities", []))
    if not required.issubset(granted_capabilities):
        return False, "The execution grant does not include the required capabilities."
    normalized_tool = str(tool or "").strip().lower()
    normalized_params = _normalized_scoped_params(normalized_tool, params)
    call_digest = _canonical_sha256(
        {"tool": normalized_tool, "params": normalized_params}
    )
    matches = [
        resource
        for resource in grant.get("resources", [])
        if int(resource.get("step_number", -1)) == int(step_number)
        and str(resource.get("tool") or "") == normalized_tool
        and str(resource.get("call_sha256") or "") == call_digest
    ]
    if len(matches) != 1:
        return False, "The tool call is outside the approved resource scope."
    claims = _CLAIMED_RESOURCE_CALLS.get()
    if claims is None:
        return False, "The execution grant claim state is unavailable."
    claim = (
        str(grant.get("approval_id") or ""),
        str(run_id or ""),
        int(step_number),
        call_digest,
    )
    if not claim[0]:
        return False, "The execution grant is missing its approval binding."
    if claim in claims:
        return False, "The approved resource call has already been claimed."
    claims.add(claim)
    return True, ""


def can_run_shell(command: str, *, admin: bool = False, cwd: str | None = None) -> dict:
    return hooks.pre_shell_command(command, cwd=cwd, admin=admin)


def can_write_file(path: str, *, source: str) -> dict:
    return hooks.pre_file_write(path, source=source)


def after_write(path: str, *, source: str) -> dict:
    return hooks.post_file_write(path, source=source)


def can_self_improve(target: str) -> dict:
    return hooks.pre_self_improve(target)
