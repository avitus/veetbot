"""A plan hashed with unsorted scopes is re-keyed after PostgreSQL reloads it (ADR-0134).

The planner reads each tool's recorded scope order back from the plan event.
jsonb keeps array order but not key order, so the order a worker recorded
before ADR-0134 survives persistence, and a planner in a new process re-keys
the plan instead of rebuilding it.
"""

from pathlib import Path

import yaml

from agent_core.bootstrap import build
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.planner import EventContextPlanner
from agent_core.tools.calculator import CalculatorTool
from agent_core.tools.registry import StaticToolRegistry
from tests.contract.support import NOW
from tests.contract.test_context_planner_contract import (
    _record_with_reversed_scopes,
    _ScopedTimeTool,
)
from tests.integration.m2_support import database_settings


async def test_a_postgres_plan_hashed_with_unsorted_scopes_keeps_its_tools() -> None:
    config = yaml.safe_load(
        (Path(__file__).parents[2] / "src/agent_core/context/plan.yaml").read_text(encoding="utf-8")
    )
    async with build(
        settings=database_settings(), storage="postgres", fixed_clock_at=NOW
    ) as composition:
        session_id = await composition.sessions.create()
        async with composition.uow_factory() as uow:
            session = await uow.sessions.get(session_id, composition.principal)
            agent = await uow.agents.get_version(session.agent_id, session.agent_version)
        configured = agent.model_copy(
            update={"enabled_tools": ["system.current_time", "math.calculate"]}
        )
        owner = composition.principal.model_copy(update={"scopes": {"email.read", "email.write"}})
        registry = StaticToolRegistry()
        registry.register(_ScopedTimeTool(composition.clock))
        model = composition.executor._resolved_model

        def planner() -> EventContextPlanner:
            """A planner with an empty cache, as in a restarted worker."""
            return EventContextPlanner(
                composition.uow_factory,
                registry,
                ConservativeTokenEstimator(),
                composition.clock,
                owner,
                config,
                policy_version="integration-policy@1",
            )

        canonical = await planner().plan(session, configured, owner, model)
        legacy = await _record_with_reversed_scopes(composition.uow_factory, canonical, configured)
        # A re-plan would now also pin math.calculate.
        registry.register(CalculatorTool())
        rekeyed = await planner().plan(session, configured, owner, model)
        async with composition.uow_factory() as uow:
            event = await uow.events.latest_before(
                session_id, (1 << 63) - 1, "context.epoch.rotated", owner
            )

    assert rekeyed.epoch == legacy.epoch + 1
    assert rekeyed.tool_specs == canonical.tool_specs
    assert rekeyed.prefix_sha256 == canonical.prefix_sha256
    assert event is not None and event.payload["reason"] == "prefix_hash_canonicalized"
