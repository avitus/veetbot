"""Agent configuration and authenticated principals."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from agent_core.domain.runs import RunLimits


class AgentSpec(BaseModel):
    """A versioned agent configuration; behavior is composed, not subclassed."""

    id: UUID
    version: str
    name: str
    instructions: str
    model_policy: str
    enabled_tools: list[str]
    enabled_skills: list[str] = Field(default_factory=list)
    policy_profile: str
    limits: RunLimits
    metadata: dict[str, Any] = Field(default_factory=dict)


class Principal(BaseModel):
    """The identity and authority stamped onto a run at submission."""

    tenant_id: str
    principal_id: str
    roles: set[str] = Field(default_factory=set)
    scopes: set[str] = Field(default_factory=set)
