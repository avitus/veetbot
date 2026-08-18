from pathlib import Path

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
from agent_core.domain.messages import ResolvedModel
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
