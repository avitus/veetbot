from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest
import yaml

import agent_core.context.planner as planner_module
from agent_core.adapters.persistence.memory import (
    InMemoryAgentRepository,
    InMemoryToolInvocationRepository,
)
from agent_core.adapters.persistence.unit_of_work import MemoryUnitOfWorkFactory
from agent_core.bootstrap import _memory_uow_repositories
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.planner import EventContextPlanner
from agent_core.context.rendering import build_prefix, deferred_index_items
from agent_core.domain.context import ContextPlan
from agent_core.domain.errors import ContextOverflow
from agent_core.domain.memory import (
    MemoryCorrection,
    RecallQuery,
    RecallResult,
    Sensitivity,
    TracedPersonContext,
)
from agent_core.domain.messages import (
    ModelLimits,
    ResolvedModel,
    SystemMessage,
    TextPart,
)
from agent_core.domain.persona import PersonaDocument, PersonaEntry, PersonaEntrySource
from agent_core.domain.skills import SessionSkillCatalog
from agent_core.memory.profiles import SnapshotProfiles
from agent_core.tools.calculator import CalculatorTool
from agent_core.tools.current_time import CurrentTimeTool
from agent_core.tools.registry import StaticToolRegistry
from agent_core.tools.tool_call import ToolCallTool
from agent_core.tools.web_fetch import WebFetchTool
from agent_core.tools.workspace.read_text import WorkspaceReadTextTool
from tests.contract.memory_fixtures import formation_stack, memory, trace
from tests.contract.support import NOW, agent, memory_stack, principal, session
from tests.unit.test_web_tools import FakeWebProvider


async def test_context_planner_persists_and_rotates_a_session_plan() -> None:
    """Plans survive reconstruction and advance epochs when prefix identity changes."""
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
    )
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)

    created = await planner.plan(session(), agent(), principal(), model)
    reloaded = await EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
    ).current(session().id)
    changed_agent = agent().model_copy(update={"instructions": "Changed instructions."})
    prefix_rotated = await planner.plan(session(), changed_agent, principal(), model)
    rotated = await planner.rotate(session().id, "contract-test")
    conflicting = created.model_copy(update={"model_id": "fake:other"}, deep=True)
    conflict_rotated = await planner._append(
        conflicting,
        "context.plan.created",
        "contract-identity-conflict",
    )

    assert created.epoch == 1
    assert reloaded == created
    assert prefix_rotated.epoch == 2
    assert prefix_rotated.prefix_sha256 != created.prefix_sha256
    assert rotated.epoch == 3
    assert rotated.prefix_sha256 == prefix_rotated.prefix_sha256
    assert conflict_rotated.epoch == 4
    assert conflict_rotated.model_id == "fake:other"


async def test_context_planner_rebuilds_when_a_snapshot_trace_disappears() -> None:
    clock, factory, _service, retriever = await formation_stack()
    belief = memory(statement="Sam prefers morning meetings")
    async with factory() as uow:
        await uow.memories.upsert_belief(belief)
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text()
    )
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
        memory_retriever=retriever,
    )
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)
    original = await planner.plan(session(), agent(), principal(), model)
    assert belief.statement in original.memory_snapshot
    async with factory() as uow:
        await uow.traces.erase_people(principal(), [], [belief.id])
    current = await planner.current(session().id)
    assert current is not None and not current.memory_snapshot
    rebuilt = await planner.plan(session(), agent(), principal(), model)
    assert rebuilt.epoch == original.epoch + 1
    assert rebuilt.snapshot_id != original.snapshot_id
    assert belief.statement in rebuilt.memory_snapshot


async def test_context_planner_reconciles_device_tools_before_reusing_a_cached_plan() -> None:
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    attached: list[UUID] = []

    async def attach(session_id: UUID, _principal) -> None:  # type: ignore[no-untyped-def]
        attached.append(session_id)

    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
        attach_device_tools=attach,
    )
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)

    created = await planner.plan(session(), agent(), principal(), model)
    reused = await planner.plan(session(), agent(), principal(), model)

    assert reused == created
    assert attached == [session().id, session().id]


async def test_context_planner_rotates_a_plan_from_the_previous_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)
    monkeypatch.setattr(planner_module, "BUILDER_VERSION", "context-builder@2")
    previous = await EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
    ).plan(session(), agent(), principal(), model)

    monkeypatch.setattr(planner_module, "BUILDER_VERSION", "context-builder@3")
    rotated = await EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
    ).plan(session(), agent(), principal(), model)

    assert previous.builder_version == "context-builder@2"
    assert rotated.builder_version == "context-builder@3"
    assert rotated.epoch == previous.epoch + 1


@pytest.mark.parametrize("legacy", [False, True])
async def test_context_authority_refresh_is_durable_and_requires_an_unpinned_run(
    legacy: bool,
) -> None:
    clock, factory, _service, _retriever = await formation_stack()
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    registry = StaticToolRegistry()
    registry.register(WorkspaceReadTextTool())
    configured_agent = agent().model_copy(update={"enabled_tools": ["workspace.read_text"]})
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)
    restricted = principal()
    owner = restricted.model_copy(update={"scopes": {"workspace.read"}})

    def planner() -> EventContextPlanner:
        return EventContextPlanner(
            factory,
            registry,
            ConservativeTokenEstimator(),
            clock,
            restricted,
            config,
            policy_version="contract-policy@1",
        )

    initial = await planner().plan(session(), configured_agent, restricted, model)
    assert initial.tool_names == ()
    if legacy:
        payload = initial.model_dump(exclude={"authority_scope_hashes"})
        payload["epoch"] = initial.epoch + 1
        initial = await planner()._append(
            ContextPlan.model_validate(payload), "context.epoch.rotated", "legacy-fixture"
        )
        assert initial.authority_scope_hashes is None

    # Reconstruction and a different principal cannot refresh a running plan.
    assert await planner().plan(session(), configured_agent, owner, model) == initial
    refreshed = await planner().plan(
        session(), configured_agent, owner, model, refresh_authorization=True
    )
    assert refreshed.tool_names == ("workspace.read_text",)
    assert refreshed.epoch == initial.epoch + 1
    assert refreshed.authority_scope_hashes is not None
    assert await planner().current(session().id) == refreshed
    assert (
        await planner().plan(session(), configured_agent, owner, model, refresh_authorization=True)
        == refreshed
    )
    async with factory() as uow:
        event = await uow.events.latest_before(
            session().id, (1 << 63) - 1, "context.epoch.rotated", restricted
        )
        assert event is not None and event.payload["reason"] == "run_authority_changed"

    # Revocation alone preserves the prefix even across run boundaries.
    assert await planner().plan(session(), configured_agent, restricted, model) == refreshed
    reduced = await planner().plan(
        session(), configured_agent, restricted, model, refresh_authorization=True
    )
    assert reduced == refreshed
    assert registry.specs_for_session(configured_agent, restricted, "test", "test") == []
    assert (
        await planner().plan(
            session(), configured_agent, restricted, model, refresh_authorization=True
        )
        == reduced
    )

    # Even an identical prefix must not make event replay alias different authority.
    changed = reduced.model_copy(update={"authority_scope_hashes": ("f" * 64,)})
    conflicted = await planner()._append(changed, "context.epoch.rotated", "authority-conflict")
    assert conflicted.epoch == reduced.epoch + 1
    assert conflicted.authority_scope_hashes == changed.authority_scope_hashes


async def test_context_planner_does_not_require_snapshot_config_without_memory() -> None:
    """A deployment without memory can plan without a memory-snapshot budget."""
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    del config["classes"]["memory_snapshot"]
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
    )

    created = await planner.plan(
        session(),
        agent(),
        principal(),
        ResolvedModel(provider="fake", model="scripted", resolved_at=NOW),
    )

    assert created.memory_snapshot == ""
    assert created.budget.retrieved_context_tokens == 2_000


async def test_context_planner_rebuilds_the_truncated_version_six_tool_roster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Older plans recover missing tools once, then persist and reuse the new epoch."""
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    registry = StaticToolRegistry()
    configured_agent = agent().model_copy(update={"enabled_tools": ["web.fetch"]})
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)

    def planner() -> EventContextPlanner:
        """Reconstruct a planner against the same durable session events."""
        return EventContextPlanner(
            factory,
            registry,
            ConservativeTokenEstimator(),
            clock,
            principal(),
            config,
            policy_version="contract-policy@1",
        )

    with monkeypatch.context() as previous_builder:
        previous_builder.setattr(planner_module, "BUILDER_VERSION", "context-builder@6")
        previous = await planner().plan(session(), configured_agent, principal(), model)
    assert previous.tool_names == ()

    registry.register(WebFetchTool(FakeWebProvider()))
    current_planner = planner()
    repaired = await current_planner.plan(session(), configured_agent, principal(), model)
    assert repaired.tool_names == ("web.fetch",)
    assert repaired.epoch == previous.epoch + 1
    assert await planner().current(session().id) == repaired
    assert await current_planner.plan(session(), configured_agent, principal(), model) == repaired


async def test_context_planner_preserves_first_occurrence_priority_at_the_tool_cap() -> None:
    """A duplicate configured name cannot demote the first requested capability."""
    clock, factory, _service, _retriever = await formation_stack()
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    config["classes"]["tool_definitions"]["max_items"] = 1
    registry = StaticToolRegistry()
    registry.register(WebFetchTool(FakeWebProvider()))
    registry.register(CurrentTimeTool(clock))
    configured_agent = agent().model_copy(
        update={"enabled_tools": ["web.fetch", "system.current_time", "web.fetch"]}
    )
    planner = EventContextPlanner(
        factory,
        registry,
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
    )

    plan = await planner.plan(
        session(),
        configured_agent,
        principal(),
        ResolvedModel(provider="fake", model="scripted", resolved_at=NOW),
    )

    assert plan.tool_names == ("web.fetch",)
    # Without tool.call nothing can be deferred, but the cut is recorded (ADR-0123).
    assert plan.deferred_tool_names == ()
    assert plan.skipped_tool_names == ("system.current_time",)


async def test_context_planner_defers_overflow_and_records_what_the_index_cannot_hold() -> None:
    """tool.call takes a slot only when something is deferred; a full index skips the rest."""
    clock, factory, _service, _retriever = await formation_stack()
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    config["classes"]["tool_definitions"]["max_items"] = 2
    config["classes"]["deferred_tool_index"]["max_items"] = 1
    registry = StaticToolRegistry()
    registry.register(ToolCallTool())
    registry.register(WebFetchTool(FakeWebProvider()))
    registry.register(CurrentTimeTool(clock))
    registry.register(CalculatorTool())
    configured_agent = agent().model_copy(
        update={
            "enabled_tools": [
                "web.fetch",
                "system.current_time",
                "math.calculate",
                "tool.call",
            ]
        }
    )
    planner = EventContextPlanner(
        factory,
        registry,
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
    )

    plan = await planner.plan(
        session(),
        configured_agent,
        principal(),
        ResolvedModel(provider="fake", model="scripted", resolved_at=NOW),
    )

    assert plan.tool_names == ("tool.call", "web.fetch")
    assert plan.deferred_tool_names == ("system.current_time",)
    assert plan.skipped_tool_names == ("math.calculate",)
    [index] = deferred_index_items(plan.deferred_tool_specs)
    assert index in build_prefix(
        configured_agent,
        plan.tool_specs,
        persona=plan.persona_text,
        deferred_tools=plan.deferred_tool_specs,
    )
    assert isinstance(index, SystemMessage)
    [part] = index.content
    assert isinstance(part, TextPart)
    assert "- system.current_time(timezone?): " in part.text


async def test_context_planner_ranks_required_session_tools_first_and_never_defers_them() -> None:
    """ADR-0130: a required tool keeps its definition over configured order and
    over the agent's own deferred list; one that cannot fit fails the plan."""
    clock, factory, _service, _retriever = await formation_stack()
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    config["classes"]["tool_definitions"]["max_items"] = 3
    registry = StaticToolRegistry()
    registry.register(ToolCallTool())
    registry.register(WebFetchTool(FakeWebProvider()))
    registry.register(CurrentTimeTool(clock))
    registry.register(CalculatorTool())
    configured_agent = agent().model_copy(
        update={
            "enabled_tools": ["web.fetch", "system.current_time", "math.calculate", "tool.call"],
            "metadata": {"deferred_tools": ["math.calculate"]},
        }
    )

    def planner(required: frozenset[str], factory: MemoryUnitOfWorkFactory) -> EventContextPlanner:
        return EventContextPlanner(
            factory,
            registry,
            ConservativeTokenEstimator(),
            clock,
            principal(),
            config,
            policy_version="contract-policy@1",
            session_required_tools=lambda _session: required,
        )

    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)
    plan = await planner(frozenset({"math.calculate", "system.current_time"}), factory).plan(
        session(), configured_agent, principal(), model
    )

    # math.calculate is named deferred by the agent, and required by the session.
    assert plan.tool_names == ("math.calculate", "system.current_time", "web.fetch")
    assert plan.deferred_tool_names == ()

    config["classes"]["tool_definitions"]["max_items"] = 2
    _clock, fresh, _service, _retriever = await formation_stack()
    required = frozenset({"math.calculate", "system.current_time", "web.fetch"})
    with pytest.raises(ValueError, match=r"system\.current_time, web\.fetch"):
        await planner(required, fresh).plan(
            session(),
            configured_agent,
            principal(),
            model,
        )


async def test_context_planner_keeps_a_plan_from_an_equivalent_earlier_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A context-builder@11 plan renders the same bytes, so it stays current (ADR-0123)."""
    clock, factory, _service, _retriever = await formation_stack()
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)

    def planner() -> EventContextPlanner:
        return EventContextPlanner(
            factory,
            StaticToolRegistry(),
            ConservativeTokenEstimator(),
            clock,
            principal(),
            config,
            policy_version="contract-policy@1",
        )

    current_version = planner_module.BUILDER_VERSION
    monkeypatch.setattr(planner_module, "BUILDER_VERSION", "context-builder@11")
    previous = await planner().plan(session(), agent(), principal(), model)
    monkeypatch.setattr(planner_module, "BUILDER_VERSION", current_version)
    reused = await planner().plan(session(), agent(), principal(), model)

    assert previous.builder_version == "context-builder@11"
    assert reused == previous


async def test_context_planner_sizes_snapshot_from_final_model_visible_bytes() -> None:
    """Snapshot selection respects the token budget of its rendered prefix content."""
    clock, factory, _service, retriever = await formation_stack()
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
        memory_retriever=retriever,
    )
    model = ResolvedModel(
        provider="fake",
        model="scripted",
        limits=ModelLimits(context_window_tokens=200_000),
        resolved_at=NOW,
    )
    records = [
        memory(
            belief_id=600 + index,
            statement=(
                f"Standing news briefing preference {index}: "
                + "compare primary reporting with independent corroboration " * 7
            ),
        ).model_copy(update={"subject": f"news preference {index}", "store_position": index})
        for index in range(1, 16)
    ]
    async with factory() as uow:
        for record in records:
            await uow.memories.upsert_belief(record)

    plan = await planner.plan(session(), agent(), principal(), model)

    assert plan.memory_snapshot
    assert plan.snapshot_id is not None
    prefix = build_prefix(
        agent(),
        plan.tool_specs,
        plan.skill_catalog,
        plan.memory_snapshot,
        persona=plan.persona_text,
    )
    prefix_without_memory = build_prefix(
        agent(),
        plan.tool_specs,
        plan.skill_catalog,
        persona=plan.persona_text,
    )
    memory_tokens = ConservativeTokenEstimator().estimate(
        prefix[len(prefix_without_memory) :], plan.model_id
    )
    assert memory_tokens <= int(config["classes"]["memory_snapshot"]["max_tokens"])
    async with factory() as uow:
        trace = await uow.traces.get(plan.snapshot_id, principal())
    assert trace.rendered == plan.memory_snapshot
    assert trace.returned == [item.belief_id for item in trace.beliefs]
    assert 0 < len(trace.returned) < len(records)
    assert set(trace.dropped_for_budget) == {record.id for record in records} - set(trace.returned)
    assert plan.prefix_tokens <= int(config["prefix"]["ceiling_tokens"])


async def test_context_planner_rotates_when_the_persona_changes() -> None:
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
    )
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)

    created = await planner.plan(session(), agent(), principal(), model)
    assert created.epoch == 1
    assert created.persona_text == ""
    assert created.persona_version == 0

    async with factory() as uow:
        await uow.personas.append_version(
            PersonaDocument(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                version=1,
                entries=(
                    PersonaEntry(
                        text="User values direct answers.",
                        source=PersonaEntrySource.USER_EDIT,
                    ),
                ),
                source=PersonaEntrySource.USER_EDIT,
                created_at=NOW,
            ),
            expected_version=0,
        )

    rotated = await planner.plan(session(), agent(), principal(), model)
    assert rotated.epoch == 2
    assert rotated.persona_version == 1
    assert rotated.persona_text == "User values direct answers."
    assert rotated.prefix_sha256 != created.prefix_sha256

    async with factory() as uow:
        event = await uow.events.latest_before(
            session().id,
            planner_module.LATEST_EVENT_BOUNDARY,
            "context.epoch.rotated",
            principal(),
        )
    assert event is not None
    assert event.payload["reason"] == "persona_changed"

    unchanged = await planner.plan(session(), agent(), principal(), model)
    assert unchanged.epoch == 2
    assert unchanged.prefix_sha256 == rotated.prefix_sha256


async def test_context_planner_rejects_a_persona_over_its_cap() -> None:
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
    )
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)

    async with factory() as uow:
        await uow.personas.append_version(
            PersonaDocument(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                version=1,
                entries=tuple(
                    PersonaEntry(
                        text=f"Truth {index}: " + "belief " * 70,
                        source=PersonaEntrySource.USER_EDIT,
                    )
                    for index in range(30)
                ),
                source=PersonaEntrySource.USER_EDIT,
                created_at=NOW,
            ),
            expected_version=0,
        )

    with pytest.raises(ContextOverflow, match="context prefix class persona exceeds its cap"):
        await planner.plan(session(), agent(), principal(), model)


class _SpyRetriever:
    def __init__(self, result: RecallResult | None = None) -> None:
        self.queries: list[RecallQuery] = []
        self.result = result

    async def corrections(
        self,
        *,
        snapshot_id: UUID,
        watermark: int,
        as_of: datetime | None = None,
    ) -> list[MemoryCorrection]:
        return []

    async def recall(
        self,
        query: RecallQuery,
        *,
        session_id: UUID,
        run_id: UUID | None = None,
        turn_id: UUID | None = None,
        moment: str = "in_turn",
        surface_id: str = "private",
        measure_rendered_tokens: Callable[[str], int] | None = None,
    ) -> RecallResult:
        del measure_rendered_tokens
        self.queries.append(query)
        return self.result or RecallResult(
            items=[],
            rendered="",
            tokens=0,
            truncated=False,
            trace_id=UUID(int=999),
            watermark=0,
        )


async def test_context_planner_preserves_people_only_snapshot() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    person = TracedPersonContext(
        record_id=UUID(int=801),
        revision=1,
        person_ids=[UUID(int=801)],
        kind="person",
        text="Maya is the owner's sister.",
        sensitivity=Sensitivity.SENSITIVE,
    )
    snapshot = RecallResult(
        items=[],
        people=[person],
        rendered='<person-context trust="memory">Maya is the owner\'s sister.</person-context>',
        tokens=24,
        truncated=False,
        trace_id=UUID(int=999),
        watermark=37,
    )
    async with factory() as uow:
        await uow.traces.record(
            trace().model_copy(update={"id": snapshot.trace_id, "people": [person]})
        )
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        yaml.safe_load(
            (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text()
        ),
        policy_version="contract-policy@1",
        memory_retriever=_SpyRetriever(snapshot),
    )
    plan = await planner.plan(
        session(),
        agent(),
        principal(),
        ResolvedModel(provider="fake", model="scripted", resolved_at=NOW),
    )
    assert plan.memory_snapshot == snapshot.rendered
    assert plan.snapshot_id == snapshot.trace_id
    assert plan.snapshot_watermark == 37


@pytest.mark.parametrize(
    (
        "metadata",
        "context_window_tokens",
        "snapshot_profiles",
        "expected_items",
        "expected_tokens",
    ),
    [
        ({}, 200_000, None, 40, 1_500),
        ({"schedule_id": str(UUID(int=701))}, 200_000, None, 80, 3_000),
        ({"run_kind": "delegated"}, 200_000, None, 15, 500),
        ({"schedule_id": str(UUID(int=702))}, 50_000, None, 80, 1_000),
        (
            {"schedule_id": str(UUID(int=703))},
            200_000,
            SnapshotProfiles.model_validate(
                {"async": {"max_items": 23, "max_tokens": 777, "max_window_ratio": 0.5}}
            ),
            23,
            777,
        ),
    ],
)
async def test_context_planner_selects_the_session_snapshot_profile(
    metadata: dict[str, str],
    context_window_tokens: int,
    snapshot_profiles: SnapshotProfiles | None,
    expected_items: int,
    expected_tokens: int,
) -> None:
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    spy = _SpyRetriever()
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
        memory_retriever=spy,
        snapshot_profiles=snapshot_profiles,
    )
    model = ResolvedModel(
        provider="fake",
        model="scripted",
        limits=ModelLimits(context_window_tokens=context_window_tokens),
        resolved_at=NOW,
    )

    plan = await planner.plan(
        session().model_copy(update={"metadata": metadata}),
        agent(),
        principal(),
        model,
    )

    assert spy.queries[0].max_items == expected_items
    assert spy.queries[0].budget_tokens == expected_tokens
    assert plan.budget.retrieved_context_tokens == expected_tokens + 2_000


@pytest.mark.parametrize(
    ("metadata", "expected_ttl"),
    [
        ({}, "default"),
        ({"schedule_id": str(UUID(int=711))}, "1h"),
        ({"run_kind": "delegated"}, "default"),
        ({"email_operational": True}, "default"),
    ],
)
async def test_context_planner_sets_the_cache_ttl_from_the_session_shape(
    metadata: dict[str, object],
    expected_ttl: str,
) -> None:
    """Only a scheduled occurrence is a long agentic loop (ADR-0132)."""
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
    )
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)

    plan = await planner.plan(
        session().model_copy(update={"metadata": metadata}), agent(), principal(), model
    )
    reloaded = await planner.current(session().id)

    assert [item.boundary for item in plan.cache_breakpoints] == [
        "after_system",
        "after_tools",
        "after_history_prefix",
    ]
    # One TTL across the frozen prefix and the default after it keep Anthropic's
    # longer-before-shorter rule; the history window moves every step.
    assert [item.ttl for item in plan.cache_breakpoints] == [expected_ttl, expected_ttl, "default"]
    assert reloaded is not None and reloaded.cache_breakpoints == plan.cache_breakpoints


async def test_context_planner_excludes_affirmed_beliefs_from_the_snapshot() -> None:
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    spy = _SpyRetriever()
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
        memory_retriever=spy,
    )
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)
    promoted = UUID("00000000-0000-0000-0000-000000000501")

    async with factory() as uow:
        await uow.personas.append_version(
            PersonaDocument(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                version=1,
                entries=(
                    PersonaEntry(
                        text="User prefers concise answers.",
                        source=PersonaEntrySource.AFFIRMATION,
                        source_belief_id=promoted,
                    ),
                ),
                source=PersonaEntrySource.AFFIRMATION,
                created_at=NOW,
            ),
            expected_version=0,
        )

    await planner.plan(session(), agent(), principal(), model)
    assert len(spy.queries) == 1
    assert spy.queries[0].exclude_ids == (promoted,)


async def test_context_planner_enforces_the_persona_item_cap() -> None:
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    config["classes"]["persona"]["max_items"] = 1
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
    )
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)
    async with factory() as uow:
        await uow.personas.append_version(
            PersonaDocument(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                version=1,
                entries=(
                    PersonaEntry(text="First truth.", source=PersonaEntrySource.USER_EDIT),
                    PersonaEntry(text="Second truth.", source=PersonaEntrySource.USER_EDIT),
                ),
                source=PersonaEntrySource.USER_EDIT,
                created_at=NOW,
            ),
            expected_version=0,
        )

    with pytest.raises(ContextOverflow, match="context prefix class persona exceeds its cap"):
        await planner.plan(session(), agent(), principal(), model)


async def test_context_planner_rotates_when_provenance_changes_under_identical_text() -> None:
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    spy = _SpyRetriever()
    planner = EventContextPlanner(
        factory,
        StaticToolRegistry(),
        ConservativeTokenEstimator(),
        clock,
        principal(),
        config,
        policy_version="contract-policy@1",
        memory_retriever=spy,
    )
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)
    promoted = UUID("00000000-0000-0000-0000-000000000501")
    text = "User prefers concise answers."

    async with factory() as uow:
        await uow.personas.append_version(
            PersonaDocument(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                version=1,
                entries=(PersonaEntry(text=text, source=PersonaEntrySource.USER_EDIT),),
                source=PersonaEntrySource.USER_EDIT,
                created_at=NOW,
            ),
            expected_version=0,
        )
    created = await planner.plan(session(), agent(), principal(), model)
    assert created.epoch == 1
    assert spy.queries[-1].exclude_ids == ()

    # Same rendered text, new provenance: the exclusion set must follow.
    async with factory() as uow:
        await uow.personas.append_version(
            PersonaDocument(
                tenant_id=principal().tenant_id,
                principal_id=principal().principal_id,
                version=2,
                entries=(
                    PersonaEntry(
                        text=text,
                        source=PersonaEntrySource.AFFIRMATION,
                        source_belief_id=promoted,
                    ),
                ),
                source=PersonaEntrySource.AFFIRMATION,
                created_at=NOW,
            ),
            expected_version=1,
        )
    rotated = await planner.plan(session(), agent(), principal(), model)
    assert rotated.epoch == 2
    assert rotated.persona_version == 2
    latest_query = spy.queries[-1]
    assert latest_query.exclude_ids == (promoted,)


class _SurfaceSpy:
    """Record every session-surface preparation the planner requests."""

    def __init__(self) -> None:
        self.opened: list[UUID] = []
        self.attached: list[UUID] = []

    async def open(self, session_id: UUID, _agent, _principal) -> SessionSkillCatalog:  # type: ignore[no-untyped-def]
        self.opened.append(session_id)
        return SessionSkillCatalog()

    async def attach(self, session_id: UUID, _principal) -> None:  # type: ignore[no-untyped-def]
        self.attached.append(session_id)


async def _surface_planner(
    spy: _SurfaceSpy, retriever: _SpyRetriever | None = None
) -> EventContextPlanner:
    clock, sessions, runs, events = await memory_stack()
    factory = MemoryUnitOfWorkFactory(
        _memory_uow_repositories(
            agents=InMemoryAgentRepository(),
            sessions=sessions,
            runs=runs,
            events=events,
            invocations=InMemoryToolInvocationRepository(runs),
            clock=clock,
        )
    )
    registry = StaticToolRegistry()
    registry.register(CurrentTimeTool(clock))
    return EventContextPlanner(
        factory,
        registry,
        ConservativeTokenEstimator(),
        clock,
        principal(),
        yaml.safe_load(
            (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(
                encoding="utf-8"
            )
        ),
        policy_version="contract-policy@1",
        skill_catalogs=spy,  # type: ignore[arg-type]
        memory_retriever=retriever,
        attach_device_tools=spy.attach,
    )


async def test_operational_email_plan_has_no_tool_surface_but_keeps_its_snapshot() -> None:
    """Typed Email work pins no tools and starts no MCP server (ADR-0104)."""
    spy = _SurfaceSpy()
    retriever = _SpyRetriever()
    planner = await _surface_planner(spy, retriever)
    operational = session().model_copy(update={"metadata": {"email_operational": True}})
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)

    created = await planner.plan(operational, agent(), principal(), model)
    reused = await planner.plan(operational, agent(), principal(), model)

    assert reused == created
    assert created.tool_names == ()
    assert created.skill_pins == () and created.skill_catalog == ()
    assert spy.opened == [] and spy.attached == []
    assert len(retriever.queries) == 1


async def test_typed_work_reuses_a_plan_without_reopening_the_session_surface() -> None:
    spy = _SurfaceSpy()
    planner = await _surface_planner(spy)
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)

    created = await planner.plan(session(), agent(), principal(), model)
    reused = await planner.plan(session(), agent(), principal(), model, prepare_surface=False)

    assert reused == created
    assert created.tool_names == ("system.current_time",)
    assert spy.opened == [session().id] and spy.attached == [session().id]


async def test_typed_work_pins_the_full_surface_when_it_creates_a_thread_session_plan() -> None:
    """Chat reuses a thread session's first plan, so typed work cannot narrow it."""
    spy = _SurfaceSpy()
    planner = await _surface_planner(spy)
    thread = session().model_copy(update={"metadata": {"email_thread_id": str(UUID(int=5))}})
    model = ResolvedModel(provider="fake", model="scripted", resolved_at=NOW)

    created = await planner.plan(thread, agent(), principal(), model, prepare_surface=False)

    assert created.tool_names == ("system.current_time",)
    assert spy.opened == [session().id] and spy.attached == [session().id]
