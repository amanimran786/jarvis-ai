"""
Jarvis RBAC — role-based access control for agent dispatch.

Four roles (high to low):
  human    — full access, no rate limit
  manager  — full dispatch, 300 rpm
  agent    — restricted to allowed_agents + allowed_tools, 120 rpm
  readonly — no dispatch, no mutations

Caller identity comes from:
  X-Jarvis-Agent-ID header  → agent-to-agent calls
  (no header)               → human operator (already authed by token middleware)

Registry is seeded from AGENT_ROSTER in config.py — no postgres required for
enforcement. Postgres is for audit persistence only.
"""
from __future__ import annotations
import logging

import threading
import time
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, Request

RoleType = Literal["human", "manager", "agent", "readonly"]

_ROLE_RANK: dict[str, int] = {"human": 3, "manager": 2, "agent": 1, "readonly": 0}


@dataclass
class AgentIdentity:
    agent_id:       str
    role:           RoleType
    allowed_tools:  list[str]   # ["*"] = all, [] = none
    allowed_agents: list[str]   # ["*"] = all, [] = none
    rate_limit_rpm: int


def _audit_rbac_denial(actor: str, target: str, reason: str) -> None:
    """Emit RBAC denial audit records without coupling enforcement to disk IO."""
    try:
        from infra.security_audit import audit_event, RBAC_DENY
        audit_event(
            RBAC_DENY,
            actor=actor or "unknown",
            target=target,
            decision="deny",
            severity="warning",
            reason=reason,
            rollback_ref="rbac_registry",
        )
    except Exception:
        logging.debug("[RBAC] silent failure in _audit_rbac_denial", exc_info=True)


# ─── Rate limiter ─────────────────────────────────────────────────────────────

class _SlidingWindow:
    """Thread-safe 60-second sliding window counter per agent_id."""

    def __init__(self):
        self._windows: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def is_allowed(self, agent_id: str, limit_rpm: int) -> bool:
        now = time.monotonic()
        cutoff = now - 60.0
        with self._lock:
            calls = [t for t in self._windows.get(agent_id, []) if t > cutoff]
            if len(calls) >= limit_rpm:
                self._windows[agent_id] = calls
                return False
            calls.append(now)
            self._windows[agent_id] = calls
            return True


# ─── Registry ─────────────────────────────────────────────────────────────────

class RBACRegistry:
    """
    In-memory RBAC registry. Seeded from AGENT_ROSTER; does not require
    postgres to be running — postgres stores audit rows, not enforce rules.
    """

    def __init__(self):
        self._identities: dict[str, AgentIdentity] = {}
        self._rl = _SlidingWindow()
        self._seed()

    def _seed(self):
        from config import AGENT_ROSTER

        self._identities["human"] = AgentIdentity(
            agent_id="human",
            role="human",
            allowed_tools=["*"],
            allowed_agents=["*"],
            rate_limit_rpm=9999,
        )
        self._identities["jarvis_manager"] = AgentIdentity(
            agent_id="jarvis_manager",
            role="manager",
            allowed_tools=["*"],
            allowed_agents=["*"],
            rate_limit_rpm=300,
        )
        for name, spec in AGENT_ROSTER.items():
            self._identities[name] = AgentIdentity(
                agent_id=name,
                role="agent",
                allowed_tools=list(spec.get("tools", [])),
                allowed_agents=[],   # agents don't cross-dispatch by default
                rate_limit_rpm=120,
            )

    # ── Public API ─────────────────────────────────────────────────────────────

    def get(self, agent_id: str) -> AgentIdentity | None:
        return self._identities.get(agent_id)

    def caller_from_request(self, request: Request) -> AgentIdentity:
        """
        Resolve caller identity. X-Jarvis-Agent-ID names the calling agent.
        Missing header → human (token auth is already verified by _guard_requests).
        """
        agent_id = request.headers.get("X-Jarvis-Agent-ID", "").strip()
        if not agent_id:
            return self._identities["human"]
        identity = self._identities.get(agent_id)
        if identity is None:
            _audit_rbac_denial(agent_id, "agent_identity", "unknown agent identity")
            raise HTTPException(
                status_code=403,
                detail=f"Unknown agent identity: '{agent_id}'",
            )
        return identity

    def check_dispatch(self, caller: AgentIdentity, target_agent: str) -> None:
        """Raise 403 if caller is not allowed to dispatch target_agent."""
        if caller.role == "readonly":
            _audit_rbac_denial(caller.agent_id, target_agent, "readonly role cannot dispatch")
            raise HTTPException(status_code=403, detail="readonly role cannot dispatch agents")
        if "*" in caller.allowed_agents:
            return
        if target_agent not in caller.allowed_agents:
            _audit_rbac_denial(
                caller.agent_id,
                target_agent,
                f"dispatch not permitted for role={caller.role}",
            )
            raise HTTPException(
                status_code=403,
                detail=(
                    f"'{caller.agent_id}' (role={caller.role}) is not permitted "
                    f"to dispatch '{target_agent}'"
                ),
            )

    def check_tool(self, caller: AgentIdentity, tool: str) -> None:
        """Raise 403 if caller is not allowed to use tool."""
        if "*" in caller.allowed_tools:
            return
        if tool not in caller.allowed_tools:
            _audit_rbac_denial(
                caller.agent_id,
                tool,
                "tool not permitted",
            )
            raise HTTPException(
                status_code=403,
                detail=f"'{caller.agent_id}' is not permitted to use tool '{tool}'",
            )

    def check_rate_limit(self, caller: AgentIdentity) -> None:
        """Raise 429 if caller has exceeded their per-minute request limit."""
        if not self._rl.is_allowed(caller.agent_id, caller.rate_limit_rpm):
            _audit_rbac_denial(
                caller.agent_id,
                "rate_limit",
                f"rate limit exceeded ({caller.rate_limit_rpm} rpm)",
            )
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Rate limit exceeded for '{caller.agent_id}' "
                    f"({caller.rate_limit_rpm} rpm)"
                ),
            )

    def enforce(self, request: Request, target_agent: str) -> AgentIdentity:
        """
        One-call convenience: resolve caller, check dispatch permission, check
        rate limit. Returns the resolved AgentIdentity on success.
        """
        caller = self.caller_from_request(request)
        self.check_dispatch(caller, target_agent)
        self.check_rate_limit(caller)
        return caller


# Module-level singleton — safe because _seed() is read-only after init.
registry = RBACRegistry()
