"""Slow optional recall must not consume a browser research run's answer time."""

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from agent_core.context import builder as context_builder
from agent_core.domain.messages import TextPart, ToolCallItem, ToolResultItem
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunStatus
from tests.contract.support import NOW, agent, principal, run
from tests.unit.test_context_recall_delta import (
    _builder,
    _checkpoint,
    _correction_line,
    _Retriever,
)


@pytest.mark.parametrize("snapshot", [True, False])
async def test_slow_base_recall_stops_without_losing_browser_evidence(
    monkeypatch: pytest.MonkeyPatch, snapshot: bool
) -> None:
    monkeypatch.setattr(context_builder, "AUTOMATIC_RECALL_SECONDS", 0.01, raising=False)
    stopped = False

    class Slow(_Retriever):
        async def recall(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal stopped
            try:
                await asyncio.sleep(0.06)
            except asyncio.CancelledError:
                stopped = True
                raise
            return await super().recall(*args, **kwargs)

    retriever = Slow()
    builder = _builder(retriever) if snapshot else _builder(retriever, snapshot_id=None)
    saved = _checkpoint("Report the browser evidence already collected.")
    saved.conversation.extend(
        [
            ToolCallItem(
                call_id="observed",
                name="browser.observe",
                arguments={},
                raw_arguments="{}",
                item_index=0,
            ),
            ToolResultItem(
                call_id="observed",
                content=[TextPart(text="Synthetic browser evidence: six linked articles.")],
                trust=TrustLevel.EXTERNAL_UNTRUSTED,
            ),
        ]
    )
    request = await builder.build(run(status=RunStatus.RUNNING), saved, agent(), principal())
    assert stopped, "automatic recall consumed the answer window instead of being cancelled"
    assert "six linked articles" in request.model_dump_json()
    assert "time allowance" in request.model_dump_json()
    assert (_correction_line() in request.model_dump_json()) is snapshot
    assert len(retriever.correction_calls) == int(snapshot)


async def test_delta_shares_the_base_deadline_and_preserves_completed_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context_builder, "AUTOMATIC_RECALL_SECONDS", 0.3, raising=False)
    stopped = False

    class SlowDelta(_Retriever):
        async def recall(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal stopped
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                stopped = True
                raise
            return await super().recall(*args, **kwargs)

    retriever = SlowDelta()
    request = await _builder(retriever).build(
        run(status=RunStatus.RUNNING), _checkpoint(), agent(), principal()
    )
    assert stopped, "delta recall renewed the time allowance after the base read"
    assert "<memory>base</memory>" in request.model_dump_json()
    assert _correction_line() in request.model_dump_json()
    assert len(retriever.queries) == 1


async def test_run_deadline_shortens_automatic_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(context_builder, "AUTOMATIC_RECALL_SECONDS", 10, raising=False)
    stopped = False

    class Slow(_Retriever):
        async def recall(self, *args: Any, **kwargs: Any) -> Any:
            nonlocal stopped
            try:
                await asyncio.sleep(0.06)
            except asyncio.CancelledError:
                stopped = True
                raise
            return await super().recall(*args, **kwargs)

    live = run(status=RunStatus.RUNNING).model_copy(
        update={"deadline_at": NOW + timedelta(seconds=0.01)}
    )
    await _builder(Slow()).build(live, _checkpoint(), agent(), principal())
    assert stopped, "optional recall ignored the run's earlier deadline"


async def test_caller_cancellation_is_not_a_recall_timeout() -> None:
    entered, stopped = asyncio.Event(), asyncio.Event()

    class Blocking(_Retriever):
        async def recall(self, *args: Any, **kwargs: Any) -> Any:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    retriever = Blocking()
    task = asyncio.create_task(
        _builder(retriever).build(
            run(status=RunStatus.RUNNING), _checkpoint(), agent(), principal()
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped.is_set()
    assert not retriever.correction_calls


async def test_slow_scope_lookup_shares_recall_deadline_and_keeps_corrections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from uuid import UUID

    monkeypatch.setattr(context_builder, "AUTOMATIC_RECALL_SECONDS", 0.01)
    stopped = False

    async def slow_scope(session_id: UUID) -> str:
        nonlocal stopped
        try:
            await asyncio.sleep(0.06)
        except asyncio.CancelledError:
            stopped = True
            raise
        return "project-a"

    retriever = _Retriever()
    builder = _builder(retriever)
    builder._session_scope = slow_scope
    request = await builder.build(
        run(status=RunStatus.RUNNING), _checkpoint(), agent(), principal()
    )
    assert stopped, "the optional scope lookup outlived the shared recall allowance"
    assert not retriever.queries
    assert _correction_line() in request.model_dump_json()
    assert "time allowance" in request.model_dump_json()


async def test_browser_run_answers_after_slow_recall_without_replaying_or_prompting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import AsyncIterator

    from agent_core.adapters.determinism import FixedClock
    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.bootstrap import build
    from agent_core.config import load_settings
    from agent_core.domain.browser import BrowserObservation
    from agent_core.domain.messages import (
        FakeModelScript,
        ModelAttempt,
        ModelEvent,
        ModelRequest,
        ResolvedModel,
        ScriptedToolCall,
        ScriptedTurn,
        StopReason,
    )
    from agent_core.domain.runs import RunLimits
    from tests.unit.test_browser_tools import FakeBrowserProvider
    from tests.unit.test_config import base_environment

    clock = FixedClock(NOW)
    stopped = False
    monkeypatch.setattr(context_builder, "AUTOMATIC_RECALL_SECONDS", 0.02)

    class ResearchBrowser(FakeBrowserProvider):
        async def navigate(self, url: str) -> BrowserObservation:
            observed = await super().navigate(url)
            clock.advance(timedelta(seconds=217))
            return observed

    provider = ResearchBrowser()
    original_stream = FakeModelProvider.stream

    async def answering(
        model: FakeModelProvider,
        request: ModelRequest,
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]:
        if provider.navigations:
            clock.advance(timedelta(seconds=22))
        async for event in original_stream(model, request, resolved, attempt):
            yield event

    monkeypatch.setattr(FakeModelProvider, "stream", answering)
    async with build(
        settings=load_settings({**base_environment(), "SANDBOX_MECHANISM": "fake"}),
        clock=clock,
        limits=RunLimits(deadline_at=NOW + timedelta(minutes=5), synthesis_reserve_steps=1),
        script=FakeModelScript(
            turns=[
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(
                            name="browser.navigate",
                            arguments={"url": "https://example.org/account"},
                        )
                    ],
                    stop_reason=StopReason.TOOL_USE,
                ),
                ScriptedTurn(
                    text="Here is the observed account overview.",
                    context_contains="Account overview",
                ),
            ]
        ),
        enabled_tools=[
            "browser.navigate",
            "browser.observe",
            "browser.act",
            "conversation.ask_user",
        ],
        browser_provider_override=provider,
    ) as app:
        original_recall = app.memory_retriever._recall

        async def slow_recall(*args: Any, **kwargs: Any) -> Any:
            nonlocal stopped
            if provider.navigations and kwargs.get("moment") == "in_turn":
                try:
                    await asyncio.sleep(0.08)
                except asyncio.CancelledError:
                    stopped = True
                    raise
                clock.advance(timedelta(seconds=78))
            return await original_recall(*args, **kwargs)

        monkeypatch.setattr(app.memory_retriever, "_recall", slow_recall)
        run_id = await app.runs.submit("Read the website and report the evidence.")
        outcome = await app.runs.wait_terminal(run_id)
        events = await app.runs.events(run_id)
        assert outcome.status is RunStatus.COMPLETED, outcome.failure
        assert outcome.final_message == "Here is the observed account overview."
        assert outcome.tool_call_count == 1
        assert not await app.approvals.list_pending(run_id=run_id)
    assert stopped
    assert provider.navigations == ["https://example.org/account"]
    assert not provider.actions
    assert not any(event.event_type == "run.waiting_for_user" for event in events)
