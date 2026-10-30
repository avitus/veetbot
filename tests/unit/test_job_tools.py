"""The agent can save code and repeatedly run it under the same private grant."""

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.cockpit.runtime import Draft202012SchemaCheck
from agent_core.application.cockpit_feeds import CockpitFeeds
from agent_core.application.job_execution import JobExecution
from agent_core.tools.jobs import JobManageTool, JobRunTool, JobToolset
from tests.contract.support import tool_context
from tests.unit.test_job_execution import Runner
from tests.unit.test_job_publication import setup


@pytest.mark.parametrize("lookup", ["owned", "missing_job", "missing_grant", "foreign"])
async def test_reapproval_card_resolves_and_redacts_stored_access(lookup: str) -> None:
    """Show legacy access before approval and never disguise an unresolved grant."""
    from unittest.mock import AsyncMock

    from tests.contract.test_job_store_contract import record

    factory, publication, _, _ = await setup()
    owner = tool_context().principal
    saved = record(owner)
    grant = saved.grants[str(UUID(int=2))]
    grant = grant.model_copy(
        update={
            "access": replace(
                grant.access,
                inputs={"repository": "example/private", "token": "private-canary"},
                hosts={"api.github.com": "github-reader"},
                feeds=frozenset({"agent.counter"}),
                model_cost="0.25",
                maximum_requests=7,
                maximum_response_bytes=1024,
                timeout_seconds=30,
            )
        }
    )
    saved = saved.model_copy(update={"grants": {str(grant.id): grant}})
    async with factory() as uow:
        await uow.jobs.put(saved, expected_version=None)
    arguments = {
        "slug": "missing-job" if lookup == "missing_job" else saved.slug,
        "revision_id": str(UUID(int=1)),
        "grant_id": str(UUID(int=999) if lookup == "missing_grant" else grant.id),
    }
    principal = (
        owner.model_copy(update={"principal_id": "foreign"}) if lookup == "foreign" else owner
    )
    view = await JobRunTool(
        JobToolset(publication, AsyncMock(spec=JobExecution))
    ).approval_view_in_session(
        arguments,
        principal=principal,
    )
    assert "private-canary" not in view.model_dump_json()
    if lookup != "owned":
        assert "unknown" in view.summary.lower()
        assert view.arguments["access_status"] == "unknown"
        return
    access = view.arguments["access"]
    assert access["hosts"] == {"api.github.com": "github-reader"}
    assert access["inputs"] == {"repository": "example/private", "token": "[REDACTED]"}
    assert access["feeds"] == ["agent.counter"]
    assert access["model_cost"] == "0.25"
    assert access["maximum_requests"] == 7
    assert access["maximum_response_bytes"] == 1024
    assert access["timeout_seconds"] == 30


async def test_agent_saves_a_job_and_reuses_its_offline_grant() -> None:
    factory, publication, _, _ = await setup()
    context = tool_context()
    context = replace(
        context,
        principal=context.principal.model_copy(update={"scopes": {"jobs.write", "jobs.run"}}),
    )
    runner = Runner()

    async def forbidden(**kwargs: Any) -> dict[str, Any]:
        pytest.fail("A pure code job contacted a remote service.")

    toolset = JobToolset(
        publication,
        JobExecution(
            uow_factory=factory,
            clock=publication.clock,
            runner=lambda timeout: runner,
            schema_check=Draft202012SchemaCheck(),
            http=forbidden,
            feeds=CockpitFeeds(
                uow_factory=factory,
                clock=publication.clock,
                schema_check=Draft202012SchemaCheck(),
                history_max=10,
            ),
        ),
    )
    managed = await JobManageTool(toolset).execute(
        {
            "operation": "create",
            "slug": "count-items",
            "source": "def main(inputs, job): return len(inputs['items'])",
            "manifest": {"title": "Count items"},
        },
        context,
    )
    assert managed.ok and managed.structured is not None
    revision_id = managed.structured["revision_id"]
    assert managed.structured["state"] == "live"
    assert await publication.flush(context.principal) == 0
    run_tool = JobRunTool(toolset)
    first = await run_tool.execute(
        {
            "slug": "count-items",
            "revision_id": revision_id,
            "access": {"inputs": {"items": [1, 2]}, "hosts": {}},
        },
        context,
    )
    assert first.ok and first.structured is not None
    assert first.structured["value"] == {"count": 2}
    grant_id = first.structured["grant_id"]
    second = await run_tool.execute(
        {"slug": "count-items", "revision_id": revision_id, "grant_id": grant_id},
        replace(context, idempotency_key="again"),
    )
    assert second.ok and second.structured is not None
    assert second.structured["grant_id"] == grant_id


@pytest.mark.parametrize("tool", [JobManageTool, JobRunTool])
def test_interrupted_job_effect_is_uncertain_instead_of_replayed(tool: Any) -> None:
    from agent_core.domain.tools import ToolInvocationStatus
    from agent_core.tools.executor import ToolRecoveryAction, tool_recovery_action
    from tests.resilience.test_tool_recovery_m2 import _invocation

    invocation = _invocation(ToolInvocationStatus.RUNNING, tool.spec.idempotency, effect_sent=True)
    assert tool_recovery_action(invocation) is ToolRecoveryAction.MARK_UNCERTAIN


async def test_trial_marks_effect_before_running_approved_program() -> None:
    from unittest.mock import AsyncMock

    from agent_core.domain.jobs import JobResult

    _, publication, _, _ = await setup()
    marked = False

    async def mark() -> None:
        nonlocal marked
        marked = True

    async def trial(**kwargs: Any) -> JobResult:
        assert marked, "Recovery must know a trial may have sent external effects."
        return JobResult({}, "", 0)

    context = tool_context()
    context = replace(
        context,
        principal=context.principal.model_copy(update={"scopes": {"jobs.write"}}),
        mark_effect_sent=mark,
    )
    execution = AsyncMock(spec=JobExecution)
    execution.trial.side_effect = trial
    result = await JobManageTool(JobToolset(publication, execution)).execute(
        {
            "operation": "trial",
            "source": "def main(inputs, job): return {}",
            "manifest": {"title": "Trial"},
        },
        context,
    )
    assert result.ok


def test_interrupted_job_schedule_update_preserves_uncertain_outcome() -> None:
    from agent_core.domain.policies import IdempotencyClass
    from agent_core.domain.tools import ToolInvocationStatus
    from agent_core.tools.executor import ToolRecoveryAction, tool_recovery_action
    from agent_core.tools.job_schedules import JobScheduleUpdateTool
    from agent_core.tools.schedule_lifecycle import CockpitScheduleUpdateTool
    from tests.resilience.test_tool_recovery_m2 import _invocation

    invocation = _invocation(
        ToolInvocationStatus.RUNNING, JobScheduleUpdateTool.spec.idempotency, effect_sent=True
    )
    assert tool_recovery_action(invocation) is ToolRecoveryAction.MARK_UNCERTAIN
    assert CockpitScheduleUpdateTool.spec.idempotency is IdempotencyClass.CONDITIONALLY_IDEMPOTENT
