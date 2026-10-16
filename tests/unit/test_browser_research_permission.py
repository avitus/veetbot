"""Owner-authorized browsing finishes without an approval for ordinary controls."""

from pathlib import Path

import pytest

from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import ToolInvocationStatus
from tests.integration.test_browser_task_grant_pipeline import (
    ANSWER,
    CONTINUE,
    DELETE,
    FINAL,
    act,
    navigate,
    pipeline,
)
from tests.scheduled_run_support import submit_scheduled_seed


@pytest.mark.parametrize("scheduled", [False, True])
@pytest.mark.parametrize("element,value", [(CONTINUE, None), (ANSWER, "agent reliability")])
async def test_owner_browsing_finishes_without_an_approval(
    tmp_path: Path, scheduled: bool, element: int, value: str | None
) -> None:
    async with pipeline(
        tmp_path, [navigate(), act(1, element, value=value), FINAL], research=True
    ) as harness:
        session_id = await harness.scheduled_session() if scheduled else harness.session_id
        run_id = (
            await submit_scheduled_seed(
                harness.composition, session_id, "Research an account to recommend."
            )
            if scheduled
            else await harness.submit("Research an account to recommend.", session_id)
        )
        result = await harness.settled(run_id)
        assert result.status is RunStatus.COMPLETED
        assert result.final_message == FINAL.text
        assert await harness.composition.approvals.list_pending(run_id=run_id) == []
        [invocation] = await harness.acts(run_id)
        assert invocation.status is ToolInvocationStatus.SUCCEEDED
        assert invocation.effect_sent_at is not None
        [authorization] = await harness.authorizations(run_id, session_id)
        assert authorization["authorization_kind"] == "browser_research"


async def test_named_consequences_still_require_approval(tmp_path: Path) -> None:
    async with pipeline(tmp_path, [navigate(), act(1, DELETE), FINAL], research=True) as harness:
        run_id = await harness.submit("Research the account.")
        assert (await harness.settled(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        assert len(await harness.composition.approvals.list_pending(run_id=run_id)) == 1
        assert not harness.runtimes[0].actions
