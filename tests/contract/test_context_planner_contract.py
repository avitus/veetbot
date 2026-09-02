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
from agent_core.domain.errors import ContextOverflow
from agent_core.domain.memory import MemoryCorrection, RecallQuery, RecallResult
from agent_core.domain.messages import ResolvedModel
from agent_core.domain.persona import PersonaDocument, PersonaEntry, PersonaEntrySource
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import NOW, agent, memory_stack, principal, session


async def test_context_planner_persists_and_rotates_a_session_plan() -> None:
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


async def test_context_planner_does_not_require_snapshot_config_without_memory() -> None:
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
    def __init__(self) -> None:
        self.queries: list[RecallQuery] = []

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
    ) -> RecallResult:
        self.queries.append(query)
        return RecallResult(
            items=[],
            rendered="",
            tokens=0,
            truncated=False,
            trace_id=UUID(int=999),
            watermark=0,
        )


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
