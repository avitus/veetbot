"""Owner-authorized browsing finishes without an approval for ordinary controls."""

from pathlib import Path
from typing import Any

import pytest

from agent_core.domain.messages import ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import RunStatus
from agent_core.domain.tools import ToolInvocationStatus
from agent_core.domain.views import TextContentBlock
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
    """Owner chat and scheduler seeds share permission for ordinary profile interactions."""
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
    """Research permission cannot authorize a visibly destructive control."""
    async with pipeline(tmp_path, [navigate(), act(1, DELETE), FINAL], research=True) as harness:
        run_id = await harness.submit("Research the account.")
        assert (await harness.settled(run_id)).status is RunStatus.WAITING_FOR_APPROVAL
        [approval] = await harness.composition.approvals.list_pending(run_id=run_id)
        assert not harness.runtimes[0].actions
        # ADR-0255: the stored record says why research mode refused.
        assert approval.research_not_covered == "excluded.destructive"


def _between(name: str) -> ScriptedTurn:
    arguments: dict[str, Any] = (
        {"question": "Which account should I start with?"}
        if name == "conversation.ask_user"
        else {"content": "account,score\n", "filename": "accounts.csv", "media_type": "text/csv"}
    )
    return ScriptedTurn(
        tool_calls=[ScriptedToolCall(name=name, arguments=arguments, call_id=f"between-{name}")],
        stop_reason=StopReason.TOOL_USE,
    )


@pytest.mark.parametrize("between", ["conversation.ask_user", "artifact.export"])
async def test_an_answered_question_or_an_export_keeps_research_permission(
    tmp_path: Path, between: str
) -> None:
    """ADR-0255: the owner's answer and a saved file bring nothing a page could
    be typed from, so the next ordinary act needs no card. A run parked on a
    question resumes in a fresh browser, so it opens the page again first, as
    the platform's own sign-in resumption does."""
    parked = between == "conversation.ask_user"
    turns = [navigate(), _between(between), *([navigate()] if parked else []), act(1, CONTINUE)]
    turns.append(FINAL)
    async with pipeline(tmp_path, turns, research=True, tools=(between,)) as harness:
        run_id = await harness.submit("Research an account to recommend.")
        if parked:
            assert (await harness.settled(run_id)).status is RunStatus.WAITING_FOR_USER
            await harness.composition.services.runs.deliver_input(
                harness.owner, run_id, [TextContentBlock(text="The newest one.")], None
            )
            await harness.drain()
        result = await harness.settled(run_id)
        assert await harness.composition.approvals.list_pending(run_id=run_id) == []
        assert result.status is RunStatus.COMPLETED, result.failure
        [authorization] = await harness.authorizations(run_id)
        assert authorization["authorization_kind"] == "browser_research"
