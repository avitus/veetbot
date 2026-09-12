"""Context selection authority survives PostgreSQL planner reconstruction."""

from dataclasses import replace
from typing import cast

import pytest

from agent_core.bootstrap import build
from agent_core.context.planner import EventContextPlanner
from agent_core.domain.context import ContextPlan
from tests.contract.support import NOW
from tests.integration.m2_support import database_settings


@pytest.mark.parametrize("legacy", [False, True])
async def test_owner_tools_refresh_after_reloading_restricted_postgres_plan(legacy: bool) -> None:
    settings = replace(database_settings(), schedule_api_enabled=True, schedule_worker_enabled=True)
    async with build(
        settings=settings,
        storage="postgres",
        fixed_clock_at=NOW,
        enabled_tools=["schedule.list", "schedule.update"],
    ) as composition:
        session_id = await composition.sessions.create()
        async with composition.uow_factory() as uow:
            session = await uow.sessions.get(session_id, composition.principal)
            agent = await uow.agents.get_version(session.agent_id, session.agent_version)
        planner = cast(EventContextPlanner, composition.executor._context_planner)
        restricted = composition.principal.model_copy(update={"scopes": set()})
        plan = await planner.plan(session, agent, restricted, composition.executor._resolved_model)
        assert plan.tool_names == ()
        if legacy:
            payload = plan.model_dump(exclude={"authority_scope_hashes"})
            payload["epoch"] = plan.epoch + 1
            plan = await planner._append(
                ContextPlan.model_validate(payload), "context.epoch.rotated", "legacy-fixture"
            )

    async with build(
        settings=settings,
        storage="postgres",
        fixed_clock_at=NOW,
        enabled_tools=["schedule.list", "schedule.update"],
    ) as restarted:
        planner = cast(EventContextPlanner, restarted.executor._context_planner)
        assert await planner.current(session_id) == plan
        model = restarted.executor._resolved_model
        # A continuation cannot repair an unknown or narrower plan mid-run.
        assert await planner.plan(session, agent, restarted.principal, model) == plan
        refreshed = await planner.plan(
            session, agent, restarted.principal, model, refresh_authorization=True
        )
        assert refreshed.epoch == plan.epoch + 1
        assert refreshed.tool_names == ("schedule.list", "schedule.update")
        assert refreshed.authority_scope_hashes is not None
        assert (
            await planner.plan(
                session, agent, restarted.principal, model, refresh_authorization=True
            )
            == refreshed
        )
        async with restarted.uow_factory() as uow:
            event = await uow.events.latest_before(
                session_id, (1 << 63) - 1, "context.epoch.rotated", restarted.principal
            )
            assert event is not None and event.payload["reason"] == "run_authority_changed"
            assert ContextPlan.model_validate(event.payload["plan"]) == refreshed
