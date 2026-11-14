"""A follow-up its run never read starts the next run or is dropped (ADR-0324)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import agent_core.runtime.loop as runtime_loop
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.bootstrap import Composition, build
from agent_core.domain.errors import ConflictError
from agent_core.domain.events import EventEnvelope
from agent_core.domain.messages import FakeModelScript, ModelPermanentError, ScriptedTurn
from agent_core.domain.runs import RunStatus
from agent_core.domain.views import SubmitResult, TextContentBlock
from tests.contract.support import NOW
from tests.gates.test_run_steering_api_adr0324 import _parked, _text
from tests.gates.test_runtime_tool_budget import _settings

FOLLOW_UP = "Use metric units."


async def _follow_up(composition: Composition, session_id: UUID, text: str) -> SubmitResult:
    result: SubmitResult = await composition.services.runs.submit(
        composition.principal, session_id, [TextContentBlock(text=text)], None, None
    )
    return result


async def _session_events(composition: Composition, session_id: UUID) -> list[EventEnvelope]:
    async with composition.uow_factory() as uow:
        return list(await uow.events.list_after(session_id, 0, composition.principal))


async def _inbox(composition: Composition, run_id: UUID) -> list[Any]:
    async with composition.uow_factory() as uow:
        return list(await uow.pending_inputs.list_for_run(run_id, composition.principal))


def _steering(clock: FixedClock, script: FakeModelScript) -> Any:
    return build(
        settings=replace(_settings(), run_steering_enabled=True),
        script=script,
        clock=clock,
        ids=SequenceIdFactory(),
    )


async def test_a_follow_up_that_arrives_during_the_final_answer_starts_the_next_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(text="About two yards."),
            ScriptedTurn(text="About two metres.", context_contains=FOLLOW_UP),
        ]
    )
    held: list[SubmitResult] = []
    original = runtime_loop.checkpoint

    async def checkpoint(context: Any, trigger: str, **options: Any) -> None:
        await original(context, trigger, **options)
        if trigger == "model_response" and not held:
            # The run has its final answer; no later step will read this.
            held.append(await _follow_up(composition, context.run.session_id, FOLLOW_UP))

    monkeypatch.setattr(runtime_loop, "checkpoint", checkpoint)
    async with _steering(FixedClock(NOW), script) as composition:
        first_id = await composition.runs.submit("How long is it?")
        first = await composition.runs.wait_terminal(first_id)
        events = await _session_events(composition, first.session_id)
        assert await _inbox(composition, first_id) == []
        (promoted,) = [
            event
            for event in events
            if event.event_type == "user.message.created" and event.run_id != first_id
        ]
        assert promoted.run_id is not None
        second = await composition.runs.wait_terminal(promoted.run_id)
        replayed = await composition.services.runs.submit(
            composition.principal, first.session_id, [TextContentBlock(text=FOLLOW_UP)], None, None
        )

    assert first.status is RunStatus.COMPLETED, first.failure
    assert first.final_message == "About two yards."
    assert held[0].run_id == first_id
    # The follow-up is the next run's message, after the first run's answer.
    assert [part["text"] for part in promoted.payload["content"]] == [FOLLOW_UP]
    assert promoted.payload["pending_input_ids"] == [str(held[0].pending_input_id)]
    kinds = [(event.event_type, event.run_id) for event in events]
    assert kinds.index(("run.completed", first_id)) < events.index(promoted)
    assert second.status is RunStatus.COMPLETED, second.failure
    assert second.final_message == "About two metres."
    assert not [event for event in events if event.event_type == "run.input.dropped"]
    # With no run working, the same words are an ordinary new message again.
    assert replayed.pending_input_id is None


async def test_a_failed_run_drops_its_unread_follow_ups(monkeypatch: pytest.MonkeyPatch) -> None:
    rejected = ModelPermanentError(
        provider="fake",
        model="scripted",
        attempt_id=UUID(int=1),
        message="The model provider rejected the request.",
        http_status=400,
        provider_code="invalid_json_schema",
    )
    held: list[SubmitResult] = []
    original = runtime_loop.build_with_pressure

    async def build_with_pressure(context: Any, step: Any) -> Any:
        if not held:
            held.append(await _follow_up(composition, context.run.session_id, FOLLOW_UP))
        return await original(context, step)

    monkeypatch.setattr(runtime_loop, "build_with_pressure", build_with_pressure)
    async with _steering(
        FixedClock(NOW), FakeModelScript(turns=[ScriptedTurn(fail_with=rejected)])
    ) as composition:
        run_id = await composition.runs.submit("How long is it?")
        run = await composition.runs.wait_terminal(run_id)
        events = await _session_events(composition, run.session_id)
        assert await _inbox(composition, run_id) == []
        active = None
        async with composition.uow_factory() as uow:
            active = await uow.runs.active_for_session(run.session_id, composition.principal)

    assert run.status is RunStatus.FAILED
    assert active is None
    (dropped,) = [event for event in events if event.event_type == "run.input.dropped"]
    assert dropped.run_id == run_id
    assert dropped.payload == {
        "pending_input_id": str(held[0].pending_input_id),
        "reason": "run_failed",
    }
    # The dropped words are not in the log: the client returns them to the composer.
    assert len([event for event in events if event.event_type == "user.message.created"]) == 1


async def test_stopping_a_parked_run_drops_what_it_held(tmp_path: Path) -> None:
    async with _parked(tmp_path, steering=True) as (composition, client, session_id, run_id):
        held = await client.post(
            f"/v1/sessions/{session_id}/messages", json=_text("Also add a date.")
        )
        cancelled = await client.post(f"/v1/runs/{run_id}/cancel")
        assert cancelled.status_code in {200, 202}
        assert (await client.get(f"/v1/runs/{run_id}")).json()["status"] == "CANCELLED"
        events = await _session_events(composition, session_id)
        assert await _inbox(composition, run_id) == []

    (dropped,) = [event for event in events if event.event_type == "run.input.dropped"]
    assert dropped.payload == {
        "pending_input_id": held.json()["pending_input_id"],
        "reason": "run_cancelled",
    }


async def test_a_message_sent_after_stop_starts_the_next_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = FixedClock(NOW)
    # The stopped run never reaches the model, so the one turn is the next run's.
    script = FakeModelScript(turns=[ScriptedTurn(text="Doing that instead.")])
    sent: dict[str, SubmitResult] = {}
    original = runtime_loop.build_with_pressure

    async def build_with_pressure(context: Any, step: Any) -> Any:
        if not sent:
            session_id = context.run.session_id
            sent["before"] = await _follow_up(composition, session_id, "Typed before Stop.")
            clock.advance(timedelta(seconds=1))
            await composition.services.runs.cancel(composition.principal, context.run.id)
            clock.advance(timedelta(seconds=1))
            sent["after"] = await _follow_up(composition, session_id, "Do this instead.")
            context.token.raise_if_cancelled()
        return await original(context, step)

    monkeypatch.setattr(runtime_loop, "build_with_pressure", build_with_pressure)
    async with _steering(clock, script) as composition:
        first_id = await composition.runs.submit("How long is it?")
        first = await composition.runs.wait_terminal(first_id)
        events = await _session_events(composition, first.session_id)
        assert await _inbox(composition, first_id) == []
        (promoted,) = [
            event
            for event in events
            if event.event_type == "user.message.created" and event.run_id != first_id
        ]
        assert promoted.run_id is not None
        second = await composition.runs.wait_terminal(promoted.run_id)

    assert first.status is RunStatus.CANCELLED
    assert [part["text"] for part in promoted.payload["content"]] == ["Do this instead."]
    (dropped,) = [event for event in events if event.event_type == "run.input.dropped"]
    assert dropped.payload == {
        "pending_input_id": str(sent["before"].pending_input_id),
        "reason": "run_cancelled",
    }
    assert second.status is RunStatus.COMPLETED, second.failure
    assert second.final_message == "Doing that instead."


async def _keyed(composition: Composition, session_id: UUID, text: str, key: str) -> SubmitResult:
    result: SubmitResult = await composition.services.runs.submit(
        composition.principal, session_id, [TextContentBlock(text=text)], key, None
    )
    return result


async def test_a_repeated_key_finds_its_follow_up_after_it_was_carried_over(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = FakeModelScript(
        turns=[ScriptedTurn(text="About two yards."), ScriptedTurn(text="Done both.")]
    )
    held: list[SubmitResult] = []
    original = runtime_loop.checkpoint

    async def checkpoint(context: Any, trigger: str, **options: Any) -> None:
        await original(context, trigger, **options)
        if trigger == "model_response" and not held:
            held.append(await _keyed(composition, context.run.session_id, "In metres.", "k1"))
            held.append(await _keyed(composition, context.run.session_id, "And feet.", "k2"))

    monkeypatch.setattr(runtime_loop, "checkpoint", checkpoint)
    async with _steering(FixedClock(NOW), script) as composition:
        first_id = await composition.runs.submit("How long is it?")
        first = await composition.runs.wait_terminal(first_id)
        session_id = first.session_id
        again = [
            await _keyed(composition, session_id, "In metres.", "k1"),
            await _keyed(composition, session_id, "And feet.", "k2"),
        ]
        changed: list[str] = []
        for key in ("k1", "k2"):
            with pytest.raises(ConflictError) as refused:
                await _keyed(composition, session_id, "Something else.", key)
            changed.append(refused.value.reason)
        # Turning the setting off must not turn an accepted follow-up into new work.
        monkeypatch.setattr(composition.services.runs, "_run_steering_enabled", False)
        after_off = await _keyed(composition, session_id, "And feet.", "k2")
        events = await _session_events(composition, session_id)

    # Both retries name the run that carried them, not a new one.
    assert [result.replayed for result in again] == [True, True]
    assert [result.pending_input_id for result in again] == [item.pending_input_id for item in held]
    assert len({result.run_id for result in again}) == 1 and again[0].run_id != first_id
    assert changed == ["idempotency_key_reused", "idempotency_key_reused"]
    assert after_off.replayed and after_off.run_id == again[0].run_id
    assert len({event.run_id for event in events if event.event_type == "run.queued"}) == 2


async def test_a_repeated_key_finds_its_follow_up_after_it_was_dropped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rejected = ModelPermanentError(
        provider="fake",
        model="scripted",
        attempt_id=UUID(int=1),
        message="The model provider rejected the request.",
        http_status=400,
        provider_code="invalid_json_schema",
    )
    held: list[SubmitResult] = []
    original = runtime_loop.build_with_pressure

    async def build_with_pressure(context: Any, step: Any) -> Any:
        if not held:
            held.append(await _keyed(composition, context.run.session_id, FOLLOW_UP, "k1"))
        return await original(context, step)

    monkeypatch.setattr(runtime_loop, "build_with_pressure", build_with_pressure)
    async with _steering(
        FixedClock(NOW), FakeModelScript(turns=[ScriptedTurn(fail_with=rejected)])
    ) as composition:
        run_id = await composition.runs.submit("How long is it?")
        run = await composition.runs.wait_terminal(run_id)
        again = await _keyed(composition, run.session_id, FOLLOW_UP, "k1")
        with pytest.raises(ConflictError) as refused:
            await _keyed(composition, run.session_id, "Something else.", "k1")
        async with composition.uow_factory() as uow:
            active = await uow.runs.active_for_session(run.session_id, composition.principal)

    # The retry reports the dropped follow-up; it does not start a run from it.
    assert again.replayed and again.run_id == run_id
    assert again.pending_input_id == held[0].pending_input_id
    assert refused.value.reason == "idempotency_key_reused"
    assert active is None
