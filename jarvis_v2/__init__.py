"""Jarvis V2: a strictly local, bounded agent runtime."""

from .agent import AgentLimits, AgentResult, LocalAgentLoop, ToolEvidence
from .config import LocalModelConfig
from .team import (
    AcceptanceContract,
    AgentAssignment,
    LocalAgentTeam,
    TeamResult,
    ToolCallContract,
    WorkerVerification,
)

__all__ = [
    "AcceptanceContract",
    "AgentAssignment",
    "AgentLimits",
    "AgentResult",
    "LocalAgentLoop",
    "LocalAgentTeam",
    "LocalModelConfig",
    "TeamResult",
    "ToolEvidence",
    "ToolCallContract",
    "WorkerVerification",
]
