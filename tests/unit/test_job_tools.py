"""The agent can save code and repeatedly run it under the same private grant."""

from dataclasses import replace
from typing import Any

import pytest

from agent_core.adapters.cockpit.runtime import Draft202012SchemaCheck
from agent_core.application.cockpit_feeds import CockpitFeeds
from agent_core.application.job_execution import JobExecution
from agent_core.tools.jobs import JobManageTool, JobRunTool, JobToolset
from tests.contract.support import tool_context
from tests.unit.test_job_execution import Runner
from tests.unit.test_job_publication import setup


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
