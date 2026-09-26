"""Agent configuration and authenticated principals."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID, uuid5

from pydantic import BaseModel, Field

from agent_core.domain.runs import RunLimits
from agent_core.domain.sessions import SESSION_BROWSER_PROFILE_METADATA_KEY, Session

# ADR-0123: configured tools this agent offers through the deferred tool index
# rather than as full definitions. Listed in agent metadata so the agent's
# content-addressed version covers it.
DEFERRED_TOOLS_METADATA_KEY = "deferred_tools"
# ADR-0130: the run limits a chat bound to a website profile runs under, in
# agent metadata so the content-addressed version pins them.
BROWSER_TASK_LIMITS_METADATA_KEY = "browser_task_limits"


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


def run_limits_for_session(agent: AgentSpec, session: Session) -> RunLimits:
    """ADR-0130: a chat bound to a website profile runs under the overlay its
    pinned agent version carries; every other chat under the agent's limits."""

    selected_profile = session.metadata.get(SESSION_BROWSER_PROFILE_METADATA_KEY)
    overlay = agent.metadata.get(BROWSER_TASK_LIMITS_METADATA_KEY)
    if isinstance(selected_profile, str) and selected_profile and overlay is not None:
        return RunLimits.model_validate(overlay)
    return agent.limits.model_copy(deep=True)


def content_addressed_agent_version(agent: AgentSpec) -> str:
    """A version derived from everything the agent does, so it cannot drift."""

    payload = agent.model_dump(mode="json", exclude={"id", "version"})
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:12]
    return f"1.0.0+h{digest}"


def chat_model_variant(agent: AgentSpec, model_policy: str) -> AgentSpec:
    """The same agent on another chat model (ADR-0119).

    A variant has its own id, derived from the agent and the policy, so it
    never becomes the latest version of the agent it varies; surfaces that
    follow the deployed agent keep following it.
    """

    if model_policy == agent.model_policy:
        return agent
    variant = agent.model_copy(
        update={
            "id": uuid5(agent.id, f"chat-model:{model_policy}"),
            "model_policy": model_policy,
        },
        deep=True,
    )
    return variant.model_copy(update={"version": content_addressed_agent_version(variant)})


class Principal(BaseModel):
    """The identity and authority stamped onto a run at submission."""

    tenant_id: str
    principal_id: str
    roles: set[str] = Field(default_factory=set)
    scopes: set[str] = Field(default_factory=set)
