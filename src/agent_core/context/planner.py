"""Durable context plans represented by idempotent session events."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from uuid import UUID

from agent_core.context.budget import ContextBudgetAllocator
from agent_core.context.estimator import canonical_json_bytes
from agent_core.context.rendering import build_prefix, prefix_bytes
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.context import ContextPlan
from agent_core.domain.errors import ConflictError, ContextOverflow
from agent_core.domain.events import NewEvent
from agent_core.domain.messages import CacheBreakpoint, ResolvedModel
from agent_core.domain.sessions import Session
from agent_core.ports.context import TokenEstimator
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.tools import ToolRegistry

BUILDER_VERSION = "context-builder@2"
PLAN_EVENT_TYPES = frozenset({"context.plan.created", "context.epoch.rotated"})


class EventContextPlanner:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        registry: ToolRegistry,
        estimator: TokenEstimator,
        clock: Clock,
        principal: Principal,
        config: Mapping[str, object],
        *,
        policy_version: str,
    ) -> None:
        self._uow_factory = uow_factory
        self._registry = registry
        self._estimator = estimator
        self._clock = clock
        self._principal = principal
        self._config = config
        self._policy_version = policy_version
        self._allocator = ContextBudgetAllocator(config)
        self._cache: dict[UUID, ContextPlan] = {}
        self._lock = asyncio.Lock()

    async def current(self, session_id: UUID) -> ContextPlan | None:
        cached = self._cache.get(session_id)
        if cached is not None:
            return cached.model_copy(deep=True)
        async with self._uow_factory() as uow:
            events = await uow.events.list_after(session_id, 0, self._principal)
        plans = [
            ContextPlan.model_validate(event.payload.get("plan"))
            for event in events
            if event.event_type in PLAN_EVENT_TYPES and isinstance(event.payload.get("plan"), dict)
        ]
        if not plans:
            return None
        plan = max(plans, key=lambda candidate: candidate.epoch)
        self._cache[session_id] = plan.model_copy(deep=True)
        return plan.model_copy(deep=True)

    async def plan(
        self,
        session: Session,
        agent: AgentSpec,
        principal: Principal,
        model: ResolvedModel,
    ) -> ContextPlan:
        async with self._lock:
            current = await self.current(session.id)
            model_id = f"{model.provider}:{model.model}"
            if current is not None:
                current_prefix = build_prefix(agent, current.tool_specs)
                current_prefix_sha256 = hashlib.sha256(
                    prefix_bytes(current_prefix, current.tool_specs)
                ).hexdigest()
                if (
                    current.model_id == model_id
                    and current.policy_version == self._policy_version
                    and current.builder_version == BUILDER_VERSION
                    and current.prefix_sha256 == current_prefix_sha256
                ):
                    return current
                return await self._create(
                    session,
                    agent,
                    principal,
                    model,
                    epoch=current.epoch + 1,
                    event_type="context.epoch.rotated",
                    reason=(
                        "model_changed"
                        if current.model_id != model_id
                        else "policy_version_changed"
                        if current.policy_version != self._policy_version
                        else "builder_version_changed"
                        if current.builder_version != BUILDER_VERSION
                        else "agent_prefix_changed"
                    ),
                )
            return await self._create(
                session,
                agent,
                principal,
                model,
                epoch=1,
                event_type="context.plan.created",
                reason="session_first_model_request",
            )

    async def rotate(self, session_id: UUID, reason: str) -> ContextPlan:
        async with self._lock:
            current = await self.current(session_id)
            if current is None:
                raise ConflictError("cannot rotate a context plan that does not exist")
            rotated = current.model_copy(
                update={"epoch": current.epoch + 1, "created_at": self._clock.now()},
                deep=True,
            )
            return await self._append(rotated, "context.epoch.rotated", reason)

    async def _create(
        self,
        session: Session,
        agent: AgentSpec,
        principal: Principal,
        model: ResolvedModel,
        *,
        epoch: int,
        event_type: str,
        reason: str,
    ) -> ContextPlan:
        classes = self._config.get("classes")
        if not isinstance(classes, dict):
            raise ValueError("context classes configuration must be a mapping")
        tool_config = classes.get("tool_definitions")
        if not isinstance(tool_config, dict):
            raise ValueError("tool-definition context configuration must be a mapping")
        maximum_tools = int(tool_config["max_items"])
        tools = self._registry.specs_for_session(
            agent,
            principal,
            profile=self._policy_version,
            environment="runtime",
        )[:maximum_tools]
        prefix = build_prefix(agent, tools)
        model_id = f"{model.provider}:{model.model}"
        framing_tokens = self._estimator.estimate(prefix[:1], model_id)
        agent_tokens = self._estimator.estimate(prefix[1:2], model_id)
        tool_tokens = self._estimator.estimate(
            prefix[2:], model_id
        ) + self._estimator.estimate_tools(tools, model_id)
        if framing_tokens > int(classes["platform_policy"]["max_tokens"]):
            raise ContextOverflow("context prefix class platform_policy exceeds its cap")
        if agent_tokens > int(classes["agent_instructions"]["max_tokens"]):
            raise ContextOverflow("context prefix class agent_instructions exceeds its cap")
        if tool_tokens > int(tool_config["max_tokens"]):
            raise ContextOverflow("context prefix class tool_definitions exceeds its cap")
        encoded_prefix = prefix_bytes(prefix, tools)
        prefix_tokens = framing_tokens + agent_tokens + tool_tokens
        prefix_config = self._config.get("prefix")
        if not isinstance(prefix_config, dict):
            raise ValueError("context prefix configuration must be a mapping")
        if prefix_tokens > int(prefix_config["ceiling_tokens"]):
            raise ContextOverflow("context frozen prefix exceeds its aggregate cap")
        budget = self._allocator.allocate(model, prefix_tokens=prefix_tokens)
        schema_bytes = canonical_json_bytes([tool.model_dump(mode="json") for tool in tools])
        plan = ContextPlan(
            session_id=session.id,
            epoch=epoch,
            prefix_sha256=hashlib.sha256(encoded_prefix).hexdigest(),
            prefix_tokens=prefix_tokens,
            model_id=model_id,
            tool_names=tuple(tool.name for tool in tools),
            tool_specs=tuple(tool.model_copy(deep=True) for tool in tools),
            tool_schema_sha256=hashlib.sha256(schema_bytes).hexdigest(),
            cache_breakpoints=(
                CacheBreakpoint(boundary="after_system"),
                CacheBreakpoint(boundary="after_tools"),
            ),
            policy_version=self._policy_version,
            builder_version=BUILDER_VERSION,
            budget=budget,
            created_at=self._clock.now(),
        )
        return await self._append(plan, event_type, reason)

    async def _append(self, plan: ContextPlan, event_type: str, reason: str) -> ContextPlan:
        derivation_key = f"context.plan:{plan.session_id}:{plan.epoch}"
        async with self._uow_factory() as uow:
            event = await uow.events.append(
                NewEvent(
                    session_id=plan.session_id,
                    run_id=None,
                    event_type=event_type,
                    actor_type="runtime",
                    payload={"plan": plan.model_dump(mode="json"), "reason": reason},
                    derivation_key=derivation_key,
                )
            )
        persisted = ContextPlan.model_validate(event.payload.get("plan"))
        if persisted.session_id != plan.session_id or persisted.epoch != plan.epoch:
            raise ConflictError("context-plan derivation resolved to a different plan")
        requested_identity = (
            plan.model_id,
            plan.policy_version,
            plan.builder_version,
            plan.prefix_sha256,
            plan.tool_schema_sha256,
        )
        persisted_identity = (
            persisted.model_id,
            persisted.policy_version,
            persisted.builder_version,
            persisted.prefix_sha256,
            persisted.tool_schema_sha256,
        )
        if persisted_identity != requested_identity:
            rotated = plan.model_copy(
                update={"epoch": plan.epoch + 1, "created_at": self._clock.now()},
                deep=True,
            )
            return await self._append(
                rotated,
                "context.epoch.rotated",
                "idempotent_plan_identity_conflict",
            )
        self._cache[plan.session_id] = persisted.model_copy(deep=True)
        return persisted.model_copy(deep=True)
