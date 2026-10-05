"""An approved upload settles after its watermark and never replays its bytes."""

import asyncio
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest

from agent_core.adapters.browser.hosted_provider import HostedBrowserProvider
from agent_core.bootstrap import build
from agent_core.config import load_settings
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.browser import BrowserAction, BrowserObservation, BrowserProviderError
from agent_core.domain.browser_upload import BrowserImageFile
from agent_core.domain.media import MediaImage
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextPart,
    ToolCallItem,
)
from agent_core.domain.runs import RunStatus, Step
from agent_core.domain.tools import ToolInvocationStatus, ToolOutcomeStatus
from agent_core.runtime.cancellation import RunCancellationToken
from agent_core.tools.browser_upload import BrowserUploadTool
from agent_core.tools.registry import RegisteredTool
from tests.contract.support import NOW, principal
from tests.unit.test_browser_upload_tool import ARGUMENTS, PNG
from tests.unit.test_config import base_environment
from tests.unit.test_hosted_browser_provider import PROFILE_ID, FakeSessions, profile


@dataclass
class GatedUploadSessions(FakeSessions):
    """Record bytes accepted by the service, then hold or lose its answer."""

    failure: str = "none"
    uploaded: list[tuple[str, BrowserImageFile, int]] = field(default_factory=list)
    upload_started: asyncio.Event = field(default_factory=asyncio.Event)
    answer_ready: asyncio.Event = field(default_factory=asyncio.Event)

    async def upload(
        self,
        lease_ref: str,
        action: BrowserAction,
        image: BrowserImageFile,
        *,
        sequence: int,
    ) -> BrowserObservation:
        assert action.expected_revision == ARGUMENTS["expected_revision"]
        assert action.ref == ARGUMENTS["ref"]
        self.uploaded.append((lease_ref, image, sequence))
        self.upload_started.set()
        await self.answer_ready.wait()
        if self.failure == "provider":
            raise BrowserProviderError("tool.browser.provider_unavailable", retryable=True)
        if self.failure == "unexpected":
            raise OSError("synthetic lost upload response")
        return BrowserObservation(url="https://example.org/compose", revision="revision-2")


@pytest.mark.parametrize("cancel", [False, True], ids=["running", "cancelled"])
@pytest.mark.parametrize("failure", ["none", "provider", "unexpected"])
async def test_upload_settles_after_watermark_without_retransferring_bytes(
    tmp_path: Path, cancel: bool, failure: str
) -> None:
    owner = principal().model_copy(
        update={"scopes": {"artifact.read", "approval.read", "approval.resolve"}}
    )
    sessions = GatedUploadSessions(failure=failure)
    provider = HostedBrowserProvider(
        principal=owner,
        profile_id=PROFILE_ID,
        allowed_origins=("https://example.org",),
        profiles=AsyncMock(return_value=profile()),
        sessions=sessions,
        now=lambda: NOW,
    )
    resolver = AsyncMock()
    resolver.resolve.return_value = (MediaImage("image/png", PNG),)
    settings = replace(
        load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        artifact_root=tmp_path,
    )
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[ScriptedToolCall(name="browser.upload", arguments=ARGUMENTS)],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text="Upload result received.", stop_reason=StopReason.END_TURN),
        ]
    )
    async with build(
        settings=settings,
        principal=owner,
        script=script,
        sequential_ids=True,
        enabled_tools=["browser.upload"],
        browser_provider_override=provider,
    ) as app:
        registered = cast(RegisteredTool, app.tool_pipeline._registry.get("browser.upload"))
        cast(BrowserUploadTool, registered.implementation)._images = resolver
        run_id = await app.runs.submit("Upload the image.")
        [approval] = await app.approvals.list_pending(run_id=run_id)
        resolver.resolve.assert_not_awaited()
        resume = asyncio.create_task(
            app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        )
        try:
            await asyncio.wait_for(sessions.upload_started.wait(), timeout=5)
            async with app.uow_factory() as uow:
                [in_flight] = await uow.invocations.list_for_run(run_id, owner)
                active_run = await uow.runs.get(run_id, owner)
                checkpoint = await uow.checkpoints.latest(run_id)
                agent = await uow.agents.get_version(active_run.agent_id, active_run.agent_version)
            assert checkpoint is not None
            assert in_flight.status is ToolInvocationStatus.RUNNING
            assert in_flight.effect_sent_at is not None
            assert in_flight.outcome is None
            if cancel:
                await app.runs.cancel(run_id)
                assert (await app.runs.get(run_id)).status is RunStatus.RUNNING
                assert not resume.done()
            assert sessions.closes == []
        finally:
            sessions.answer_ready.set()
            await asyncio.wait_for(resume, timeout=5)

        run = await app.runs.wait_terminal(run_id)
        assert run.status is (RunStatus.CANCELLED if cancel else RunStatus.COMPLETED)
        async with app.uow_factory() as uow:
            [settled] = await uow.invocations.list_for_run(run_id, owner)
        uncertain = failure != "none"
        assert settled.status is (
            ToolInvocationStatus.UNCERTAIN if uncertain else ToolInvocationStatus.SUCCEEDED
        )
        assert settled.effect_sent_at == in_flight.effect_sent_at
        assert settled.outcome is not None
        assert settled.outcome.status is (
            ToolOutcomeStatus.UNCERTAIN if uncertain else ToolOutcomeStatus.SUCCEEDED
        )
        assert settled.outcome.reason_code == (
            "tool.browser.outcome_unknown" if uncertain else "tool.succeeded"
        )
        assert not settled.outcome.retryable
        assert settled.result_item is not None
        assert settled.result_item.is_error is uncertain
        assert not provider.holds_lease
        assert sessions.closes == [sessions.uploaded[0][0]]

        events = await app.runs.events(run_id)
        [reported] = [
            event
            for event in events
            if event.event_type == ("tool.call.uncertain" if uncertain else "tool.call.completed")
        ]
        assert reported.payload["reason_code"] == settled.outcome.reason_code
        assert reported.payload["result_item"] == settled.result_item.model_dump(mode="json")
        if cancel:
            [cancelled] = [event for event in events if event.event_type == "run.cancelled"]
            assert events.index(reported) < events.index(cancelled)

        # Recovery returns success from storage; the identical-uncertain guard
        # denies a repeat. Neither path may resolve or send the image again.
        replay = await app.tool_pipeline.dispatch(
            run=active_run,
            checkpoint=checkpoint,
            tool_calls=[
                ToolCallItem(
                    call_id=settled.call_id,
                    item_index=0,
                    name=settled.tool_name,
                    arguments=ARGUMENTS,
                    raw_arguments=settled.raw_arguments,
                )
            ],
            principal=owner,
            step=Step(run_id=run_id, step_number=settled.step_number, started_at=app.clock.now()),
            agent=agent,
            token=RunCancellationToken(app.clock, None),
        )
        if uncertain:
            [refusal] = replay
            assert refusal.is_error
            [part] = refusal.content
            assert isinstance(part, TextPart)
            outcome = json.loads(part.text)
            assert outcome["status"] == "denied"
            assert outcome["reason_code"] == "tool.outcome_unknown"
            assert not outcome["retryable"]
        else:
            assert replay == [settled.result_item]
        async with app.uow_factory() as uow:
            assert await uow.invocations.list_for_run(run_id, owner) == [settled]
        resolver.resolve.assert_awaited_once()
        assert len(sessions.uploaded) == 1
        assert sessions.uploaded[0][1].image.data == PNG
        assert sessions.uploaded[0][2] == 1
        assert len(sessions.acquisitions) == 1
        assert sessions.closes == [sessions.uploaded[0][0]]
