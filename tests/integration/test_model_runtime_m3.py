"""Durable Milestone 3 model pin, continuation, and resume verification."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider
from agent_core.bootstrap import build
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker, MaintenanceWorker
from tests.contract.model_fixtures import ScriptedRawSource, anthropic_text_events
from tests.contract.support import NOW
from tests.integration.m2_support import database_settings


class InjectedWorkerCrash(BaseException):
    pass


def thinking_tool_events() -> list[dict[str, Any]]:
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg-reasoning-tool",
                "model": "claude-opus-5",
                "usage": {"input_tokens": 12, "output_tokens": 1},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "thinking", "thinking": "", "signature": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": "private plan"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": "signed-state"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {
                "type": "tool_use",
                "id": "resume-call-id",
                "name": "math.calculate",
            },
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {
                "type": "input_json_delta",
                "partial_json": '{"expression":"17 * 23"}',
            },
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 8},
        },
        {"type": "message_stop"},
    ]


async def test_pin_and_opaque_continuation_survive_worker_reconstruction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.runtime import loop as loop_module

    clock = FixedClock(NOW)
    first_source = ScriptedRawSource([thinking_tool_events()])
    first_provider = AnthropicMessagesProvider(event_source=first_source)
    original_checkpoint = loop_module.checkpoint
    crashed = False

    async def crash_after_tool_checkpoint(context: Any, trigger: str) -> None:
        nonlocal crashed
        await original_checkpoint(context, trigger)
        if trigger == "tool_call" and not crashed:
            crashed = True
            raise InjectedWorkerCrash

    async with build(
        settings=database_settings(),
        storage="postgres",
        clock=clock,
        model_policy="flagship",
        model_provider_overrides={"anthropic": first_provider},
    ) as composition:
        run_id = await composition.runs.submit("calculate with a pinned provider")
        monkeypatch.setattr(loop_module, "checkpoint", crash_after_tool_checkpoint)
        worker = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=clock,
            worker_id="m3-crashed-worker",
        )
        with pytest.raises(InjectedWorkerCrash):
            await worker.run_once()
        async with composition.uow_factory() as uow:
            interrupted = await uow.checkpoints.latest(run_id)
        assert interrupted is not None
        assert interrupted.provider_pin is not None
        assert interrupted.provider_pin.provider == "anthropic"
        assert interrupted.provider_continuation is not None
        assert interrupted.provider_continuation.provider == "anthropic"
        assert (
            interrupted.provider_continuation.opaque_items[0]["provider_payload"]["signature"]
            == "signed-state"
        )
        initial_pin = interrupted.provider_pin.model_copy(deep=True)

    monkeypatch.setattr(loop_module, "checkpoint", original_checkpoint)
    clock.advance(timedelta(seconds=31))
    resumed_source = ScriptedRawSource([anthropic_text_events("391")])
    resumed_provider = AnthropicMessagesProvider(event_source=resumed_source)
    async with build(
        settings=database_settings(),
        storage="postgres",
        clock=clock,
        model_provider_overrides={"anthropic": resumed_provider},
    ) as composition:
        maintenance = MaintenanceWorker(uow_factory=composition.uow_factory, clock=clock)
        assert await maintenance.run_once() == 1
        clock.advance(timedelta(seconds=2))
        recovery = DurableWorker(
            uow_factory=composition.uow_factory,
            executor=composition.executor,
            clock=clock,
            worker_id="m3-recovery-worker",
        )
        assert await recovery.run_once()
        recovered = await composition.runs.get(run_id)
        async with composition.uow_factory() as uow:
            final_checkpoint = await uow.checkpoints.latest(run_id)

    assert recovered.status is RunStatus.COMPLETED
    assert recovered.final_message == "391"
    assert final_checkpoint is not None
    assert final_checkpoint.provider_pin == initial_pin
    assert final_checkpoint.provider_continuation is None
    assert "signed-state" in str(resumed_source.requests[0])
    assert "resume-call-id" in str(resumed_source.requests[0])
