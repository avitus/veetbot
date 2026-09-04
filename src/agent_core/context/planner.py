"""Durable context plans represented by idempotent session events."""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from uuid import UUID
from weakref import WeakValueDictionary

from agent_core.context.budget import ContextBudgetAllocator
from agent_core.context.estimator import canonical_json_bytes
from agent_core.context.rendering import build_prefix, prefix_bytes
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.context import ContextPlan
from agent_core.domain.errors import ConflictError, ContextOverflow
from agent_core.domain.events import NewEvent
from agent_core.domain.hazards import contains_injection_pattern
from agent_core.domain.memory import RecallMoment, RecallProfile, RecallQuery, Sensitivity
from agent_core.domain.messages import CacheBreakpoint, ResolvedModel
from agent_core.domain.persona import render_persona
from agent_core.domain.runs import RunKind
from agent_core.domain.sessions import Session, project_scope
from agent_core.domain.tools import ToolSpec
from agent_core.memory.profiles import SnapshotProfile, SnapshotProfiles
from agent_core.ports.context import TokenEstimator
from agent_core.ports.determinism import Clock
from agent_core.ports.memory import MemoryRetriever
from agent_core.ports.persistence import UnitOfWorkFactory
from agent_core.ports.skills import SkillCatalog
from agent_core.ports.tools import ToolRegistry

BUILDER_VERSION = "context-builder@5"
PLAN_EVENT_TYPES = frozenset({"context.plan.created", "context.epoch.rotated"})
LATEST_EVENT_BOUNDARY = (1 << 63) - 1
MAX_PLAN_APPEND_ATTEMPTS = 16
_SKILL_LOAD_TOOL_NAME = "skill.load"
_SKILL_REVIEW_RUN_KIND = "skill_review"

type SessionToolFilter = Callable[[Session, Sequence[ToolSpec]], list[ToolSpec]]
type DeviceToolAttach = Callable[[UUID, Principal], Awaitable[None]]


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
        skill_catalogs: SkillCatalog | None = None,
        memory_retriever: MemoryRetriever | None = None,
        session_tool_filter: SessionToolFilter | None = None,
        attach_device_tools: DeviceToolAttach | None = None,
        snapshot_profiles: SnapshotProfiles | None = None,
        cache_capacity: int = 1_024,
    ) -> None:
        if cache_capacity <= 0:
            raise ValueError("context-plan cache capacity must be positive")
        self._uow_factory = uow_factory
        self._registry = registry
        self._estimator = estimator
        self._clock = clock
        self._principal = principal
        self._config = config
        self._policy_version = policy_version
        self._skill_catalogs = skill_catalogs
        self._memory_retriever = memory_retriever
        self._session_tool_filter = session_tool_filter
        self._attach_device_tools = attach_device_tools
        self._snapshot_profiles = (
            SnapshotProfiles() if snapshot_profiles is None else snapshot_profiles
        )
        self._allocator = ContextBudgetAllocator(config)
        self._cache_capacity = cache_capacity
        self._cache: OrderedDict[UUID, ContextPlan] = OrderedDict()
        self._locks: WeakValueDictionary[UUID, asyncio.Lock] = WeakValueDictionary()

    def _session_lock(self, session_id: UUID) -> asyncio.Lock:
        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock

    def _remember(self, plan: ContextPlan) -> None:
        self._cache[plan.session_id] = plan.model_copy(deep=True)
        self._cache.move_to_end(plan.session_id)
        if len(self._cache) > self._cache_capacity:
            self._cache.popitem(last=False)

    async def current(self, session_id: UUID) -> ContextPlan | None:
        cached = self._cache.get(session_id)
        if cached is not None:
            self._cache.move_to_end(session_id)
            return cached.model_copy(deep=True)
        async with self._uow_factory() as uow:
            events = [
                event
                for event_type in sorted(PLAN_EVENT_TYPES)
                if (
                    event := await uow.events.latest_before(
                        session_id,
                        LATEST_EVENT_BOUNDARY,
                        event_type,
                        self._principal,
                    )
                )
                is not None
            ]
        plans = [
            ContextPlan.model_validate(event.payload.get("plan"))
            for event in events
            if isinstance(event.payload.get("plan"), dict)
        ]
        if not plans:
            return None
        plan = max(plans, key=lambda candidate: candidate.epoch)
        self._remember(plan)
        return plan.model_copy(deep=True)

    async def plan(
        self,
        session: Session,
        agent: AgentSpec,
        principal: Principal,
        model: ResolvedModel,
    ) -> ContextPlan:
        async with self._session_lock(session.id):
            (
                persona_text,
                persona_version,
                persona_affirmed,
                persona_items,
            ) = await self._active_persona(principal)
            current = await self.current(session.id)
            model_id = f"{model.provider}:{model.model}"
            if current is not None:
                if self._attach_device_tools is not None:
                    await self._attach_device_tools(session.id, principal)
                if self._skill_catalogs is not None:
                    await self._skill_catalogs.open(session.id, agent, principal)
                current_prefix = build_prefix(
                    agent,
                    current.tool_specs,
                    current.skill_catalog,
                    current.memory_snapshot,
                    persona=current.persona_text,
                )
                current_prefix_sha256 = hashlib.sha256(
                    prefix_bytes(current_prefix, current.tool_specs)
                ).hexdigest()
                if (
                    current.model_id == model_id
                    and current.policy_version == self._policy_version
                    and current.builder_version == BUILDER_VERSION
                    and current.prefix_sha256 == current_prefix_sha256
                    and current.persona_text == persona_text
                    and current.persona_version == persona_version
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
                        else "persona_changed"
                        if current.persona_text != persona_text
                        or current.persona_version != persona_version
                        else "agent_prefix_changed"
                    ),
                    persona_text=persona_text,
                    persona_version=persona_version,
                    persona_affirmed=persona_affirmed,
                    persona_items=persona_items,
                )
            return await self._create(
                session,
                agent,
                principal,
                model,
                epoch=1,
                event_type="context.plan.created",
                reason="session_first_model_request",
                persona_text=persona_text,
                persona_version=persona_version,
                persona_affirmed=persona_affirmed,
                persona_items=persona_items,
            )

    async def rotate(self, session_id: UUID, reason: str) -> ContextPlan:
        async with self._session_lock(session_id):
            current = await self.current(session_id)
            if current is None:
                raise ConflictError("cannot rotate a context plan that does not exist")
            rotated = current.model_copy(
                update={"epoch": current.epoch + 1, "created_at": self._clock.now()},
                deep=True,
            )
            return await self._append(rotated, "context.epoch.rotated", reason)

    async def _active_persona(self, principal: Principal) -> tuple[str, int, tuple[UUID, ...], int]:
        """The rendered persona row and its pinned version for this principal.

        Rendering filters entries above the session surface's sensitivity
        ceiling exactly once, here, so the row is byte-stable for the life of
        the epoch. Rotation compares rendered text rather than versions: a
        version bump that changes no visible byte changes no prefix.
        """

        async with self._uow_factory() as uow:
            document = await uow.personas.active(principal)
        if document is None:
            return "", 0, (), 0
        # The load-time injection scan is the guarantee (the write surfaces
        # scan too, but only this pass covers text stored before a pattern
        # existed). A poisoned entry renders as a placeholder, never as
        # instruction text.
        screened = document.model_copy(
            update={
                "entries": tuple(
                    entry.model_copy(update={"text": "[BLOCKED]"})
                    if contains_injection_pattern(entry.text)
                    else entry
                    for entry in document.entries
                )
            }
        )
        rendered = render_persona(screened, ceiling=Sensitivity.RESTRICTED)
        return (
            rendered,
            document.version,
            document.affirmed_belief_ids,
            len(rendered.splitlines()) if rendered else 0,
        )

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
        persona_text: str = "",
        persona_version: int = 0,
        persona_affirmed: tuple[UUID, ...] = (),
        persona_items: int = 0,
    ) -> ContextPlan:
        classes = self._config.get("classes")
        if not isinstance(classes, dict):
            raise ValueError("context classes configuration must be a mapping")
        tool_config = classes.get("tool_definitions")
        if not isinstance(tool_config, dict):
            raise ValueError("tool-definition context configuration must be a mapping")
        maximum_tools = int(tool_config["max_items"])
        # Opening the catalog also performs MCP discovery. It must happen before
        # tool advertisement so the same frozen plan pins both surfaces.
        catalog = (
            None
            if self._skill_catalogs is None
            else await self._skill_catalogs.open(session.id, agent, principal)
        )
        # Device attach reconciles capability-derived registrations against the
        # devices present now, for the same reason and at the same point.
        if self._attach_device_tools is not None:
            await self._attach_device_tools(session.id, principal)
        tools = self._registry.specs_for_session(
            agent,
            principal,
            profile=self._policy_version,
            environment="runtime",
        )
        if self._session_tool_filter is not None:
            tools = self._session_tool_filter(session, tools)
        if (
            catalog is not None
            and not catalog.entries
            and agent.metadata.get("run_kind") != _SKILL_REVIEW_RUN_KIND
        ):
            tools = [tool for tool in tools if tool.name != _SKILL_LOAD_TOOL_NAME]
        tools = tools[:maximum_tools]
        catalog_metadata = (
            () if catalog is None else tuple(entry.metadata for entry in catalog.entries)
        )
        model_id = f"{model.provider}:{model.model}"
        base_prefix = build_prefix(agent, tools)
        persona_prefix = build_prefix(agent, tools, persona=persona_text)
        catalog_prefix = build_prefix(agent, tools, catalog_metadata, persona=persona_text)
        memory_config = classes.get("memory_snapshot")
        if self._memory_retriever is not None and not isinstance(memory_config, dict):
            raise ValueError("memory-snapshot context configuration must be a mapping")
        if self._memory_retriever is None:
            snapshot = None
            memory_token_cap = 0
        else:
            assert isinstance(memory_config, dict)
            snapshot_profile = self._snapshot_profile(session, memory_config)
            memory_token_cap = min(
                snapshot_profile.max_tokens,
                max(
                    1,
                    int(model.limits.context_window_tokens * snapshot_profile.max_window_ratio),
                ),
            )

            def measure_memory_tokens(rendered: str) -> int:
                candidate_prefix = build_prefix(
                    agent,
                    tools,
                    catalog_metadata,
                    rendered,
                    persona=persona_text,
                )
                return self._estimator.estimate(candidate_prefix[len(catalog_prefix) :], model_id)

            snapshot = await self._memory_retriever.recall(
                RecallQuery(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    current_scope=project_scope(session.metadata),
                    profile=RecallProfile.CORE,
                    budget_tokens=memory_token_cap,
                    max_items=snapshot_profile.max_items,
                    min_score=0.1,
                    sensitivity_ceiling=Sensitivity.RESTRICTED,
                    # Beliefs the persona row already carries never occupy a
                    # snapshot slot (persona-surface.md).
                    exclude_ids=persona_affirmed,
                ),
                session_id=session.id,
                moment=RecallMoment.SNAPSHOT.value,
                measure_rendered_tokens=measure_memory_tokens,
            )
        memory_snapshot = "" if snapshot is None or not snapshot.items else snapshot.rendered
        prefix = build_prefix(agent, tools, catalog_metadata, memory_snapshot, persona=persona_text)
        framing_tokens = self._estimator.estimate(prefix[:1], model_id)
        agent_tokens = self._estimator.estimate(prefix[1:2], model_id)
        # The persona row sits at index 2 of the full prefix when present;
        # base_prefix never carries it, so the tool slice below is unshifted.
        persona_prefix_items = len(persona_prefix) - len(base_prefix)
        persona_tokens = (
            self._estimator.estimate(prefix[2 : 2 + persona_prefix_items], model_id)
            if persona_prefix_items
            else 0
        )
        tool_tokens = self._estimator.estimate(
            base_prefix[2:], model_id
        ) + self._estimator.estimate_tools(tools, model_id)
        skill_catalog_tokens = self._estimator.estimate(
            catalog_prefix[len(persona_prefix) :], model_id
        )
        memory_tokens = self._estimator.estimate(prefix[len(catalog_prefix) :], model_id)
        if framing_tokens > int(classes["platform_policy"]["max_tokens"]):
            raise ContextOverflow("context prefix class platform_policy exceeds its cap")
        if agent_tokens > int(classes["agent_instructions"]["max_tokens"]):
            raise ContextOverflow("context prefix class agent_instructions exceeds its cap")
        if persona_tokens:
            persona_config = classes.get("persona")
            if not isinstance(persona_config, dict):
                raise ValueError("persona context configuration must be a mapping")
            maximum_entries = int(persona_config["max_items"])
            if maximum_entries < 1:
                raise ValueError("persona max_items must be at least 1")
            if persona_items > maximum_entries:
                raise ContextOverflow("context prefix class persona exceeds its cap")
            if persona_tokens > int(persona_config["max_tokens"]):
                raise ContextOverflow("context prefix class persona exceeds its cap")
        if tool_tokens > int(tool_config["max_tokens"]):
            raise ContextOverflow("context prefix class tool_definitions exceeds its cap")
        skill_config = classes.get("skill_catalog")
        if not isinstance(skill_config, dict):
            raise ValueError("skill-catalog context configuration must be a mapping")
        if skill_catalog_tokens > int(skill_config["max_tokens"]):
            raise ContextOverflow("context prefix class skill_catalog exceeds its cap")
        if isinstance(memory_config, dict) and memory_tokens > memory_token_cap:
            raise ContextOverflow("context prefix class memory_snapshot exceeds its cap")
        encoded_prefix = prefix_bytes(prefix, tools)
        prefix_tokens = (
            framing_tokens
            + agent_tokens
            + persona_tokens
            + tool_tokens
            + skill_catalog_tokens
            + memory_tokens
        )
        prefix_config = self._config.get("prefix")
        if not isinstance(prefix_config, dict):
            raise ValueError("context prefix configuration must be a mapping")
        if prefix_tokens > int(prefix_config["ceiling_tokens"]):
            raise ContextOverflow("context frozen prefix exceeds its aggregate cap")
        budget = self._allocator.allocate(
            model,
            prefix_tokens=prefix_tokens,
            memory_snapshot_tokens=memory_token_cap,
        )
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
            snapshot_id=None if snapshot is None else snapshot.trace_id,
            snapshot_watermark=0 if snapshot is None else snapshot.watermark,
            memory_snapshot=memory_snapshot,
            persona_text=persona_text,
            persona_version=persona_version,
            skill_pins=() if catalog is None else catalog.pins,
            skill_catalog=catalog_metadata,
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

    def _snapshot_profile(
        self,
        session: Session,
        interactive_config: Mapping[str, object],
    ) -> SnapshotProfile:
        if session.metadata.get("run_kind") == RunKind.DELEGATED.value:
            return self._snapshot_profiles.child
        if "schedule_id" in session.metadata:
            return self._snapshot_profiles.async_
        return SnapshotProfile.model_validate(
            {
                "max_items": interactive_config["max_items"],
                "max_tokens": interactive_config["max_tokens"],
                "max_window_ratio": interactive_config["max_window_ratio"],
            }
        )

    async def _append(self, plan: ContextPlan, event_type: str, reason: str) -> ContextPlan:
        candidate = plan
        candidate_event_type = event_type
        candidate_reason = reason
        for _attempt in range(MAX_PLAN_APPEND_ATTEMPTS):
            derivation_key = f"context.plan:{candidate.session_id}:{candidate.epoch}"
            async with self._uow_factory() as uow:
                event = await uow.events.append(
                    NewEvent(
                        session_id=candidate.session_id,
                        run_id=None,
                        event_type=candidate_event_type,
                        actor_type="runtime",
                        payload={
                            "plan": candidate.model_dump(mode="json"),
                            "reason": candidate_reason,
                        },
                        derivation_key=derivation_key,
                    )
                )
            persisted = ContextPlan.model_validate(event.payload.get("plan"))
            if persisted.session_id != candidate.session_id or persisted.epoch != candidate.epoch:
                raise ConflictError("context-plan derivation resolved to a different plan")
            requested_identity = (
                candidate.model_id,
                candidate.policy_version,
                candidate.builder_version,
                candidate.prefix_sha256,
                candidate.tool_schema_sha256,
            )
            persisted_identity = (
                persisted.model_id,
                persisted.policy_version,
                persisted.builder_version,
                persisted.prefix_sha256,
                persisted.tool_schema_sha256,
            )
            if persisted_identity == requested_identity:
                self._remember(persisted)
                return persisted.model_copy(deep=True)
            candidate = candidate.model_copy(
                update={"epoch": candidate.epoch + 1, "created_at": self._clock.now()},
                deep=True,
            )
            candidate_event_type = "context.epoch.rotated"
            candidate_reason = "idempotent_plan_identity_conflict"
        raise ConflictError("context-plan identity contention exceeded the retry limit")
