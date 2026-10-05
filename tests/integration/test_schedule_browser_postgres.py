"""A stored schedule drives the session-bound browser through the run worker."""

from dataclasses import replace
from typing import cast

import pytest

from agent_core.adapters.browser.hosted_provider import (
    HostedBrowserProvider,
    SessionBoundHostedBrowserProvider,
)
from agent_core.adapters.identity import StaticSchedulePrincipalDirectory
from agent_core.adapters.schedule_admission import AllowScheduleAdmissionController
from agent_core.bootstrap import build
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import RunStatus
from agent_core.domain.schedules import ScheduleDefinition
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.runtime.checkpoints import DurableCheckpointSeeder
from agent_core.scheduling.materializer import ScheduleMaterializer
from agent_core.tools.browser_navigate import BrowserNavigateTool
from agent_core.tools.registry import RegisteredTool
from tests.gates.test_schedule_api_m11 import NOW, _definition
from tests.integration.m2_support import database_settings
from tests.unit.test_browser_composition import (
    PROFILE_ID,
    seed_browser_authority,
    session_bound_hosted_settings,
)
from tests.unit.test_hosted_browser_provider import FakeSessions


@pytest.mark.parametrize("fixed_profile", [False, True])
async def test_persisted_browser_schedule_reads_through_its_owned_profile(
    fixed_profile: bool,
) -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="browser.navigate",
                        arguments={"url": "https://example.org/home"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(
                text="Briefing from the observed home page.", stop_reason=StopReason.END_TURN
            ),
        ]
    )
    settings = replace(
        session_bound_hosted_settings(),
        database_url=database_settings().database_url,
        browser_profile_id=PROFILE_ID if fixed_profile else None,
        browser_allowed_origins=("https://example.org",) if fixed_profile else (),
    )
    async with build(
        settings=settings, storage="postgres", script=script, fixed_clock_at=NOW
    ) as composition:
        await seed_browser_authority(composition)
        session_id = await composition.sessions.create()
        async with composition.uow_factory() as uow:
            source = await uow.sessions.get(session_id, composition.principal)
        definition = ScheduleDefinition.model_validate(
            {
                **_definition(),
                "agent_id": str(source.agent_id),
                "agent_version": source.agent_version,
                "requested_scopes": ["browser.profile.read"],
                "browser_profile_id": str(PROFILE_ID),
                "cadence": {"kind": "ONCE", "at": NOW.isoformat()},
            }
        )
        record = await composition.services.schedules.create(
            composition.principal, definition, "browser"
        )
        async with composition.uow_factory() as uow:
            restored = await uow.schedules.get_revision(
                record.schedule.id, 1, composition.principal
            )
            assert restored.browser_profile_id == PROFILE_ID
        materializer = ScheduleMaterializer(
            uow_factory=composition.uow_factory,
            principals=StaticSchedulePrincipalDirectory(composition.principal),
            admission=AllowScheduleAdmissionController(),
            clock=composition.clock,
            ids=composition.ids,
            seed_checkpoint=DurableCheckpointSeeder(composition.clock),
        )
        occurrence = await materializer.materialize(record.schedule.id)
        assert occurrence is not None and occurrence.run_id is not None
        # Keep the real session selector, profile loader, policy and tool pipeline;
        # replace only the isolated service's network boundary.
        registered = cast(
            RegisteredTool, composition.tool_pipeline._registry.get("browser.navigate")
        )
        tool = cast(BrowserNavigateTool, registered.implementation)
        provider = cast(HostedBrowserProvider | SessionBoundHostedBrowserProvider, tool._provider)
        sessions = FakeSessions()
        provider._sessions = sessions
        await composition.executor.execute(occurrence.run_id)
        async with composition.uow_factory() as uow:
            run = await uow.runs.get(occurrence.run_id, composition.principal)
            invocations = await uow.invocations.list_for_run(run.id, composition.principal)
        assert run.status is RunStatus.COMPLETED, run.failure
        assert run.tool_call_count == 1
        assert len(invocations) == 1 and invocations[0].tool_name == "browser.navigate"
        assert invocations[0].status is ToolInvocationStatus.SUCCEEDED, invocations[0].outcome
        assert sessions.acquisitions == [(PROFILE_ID, run.id, 1)]
        assert run.principal_scopes == {"browser.profile.read"}
        assert run.limits.max_cost == definition.limits.max_cost
