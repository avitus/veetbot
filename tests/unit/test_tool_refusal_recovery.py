"""A refused call remains one result across successive approval resumes."""

from dataclasses import replace

import pytest

from agent_core.bootstrap import Composition, build
from agent_core.context.history import validate_tool_pairs
from agent_core.domain.approvals import ApprovalResolutionType
from agent_core.domain.messages import (
    FakeModelScript,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    ToolResultItem,
)
from agent_core.domain.runs import RunStatus
from agent_core.runtime.worker import DurableWorker
from tests.integration.m2_support import memory_settings


def refusal_script(refused: ScriptedToolCall) -> FakeModelScript:
    def write(call_id: str) -> ScriptedToolCall:
        return ScriptedToolCall(
            name="demo.external_write",
            arguments={"destination": "demo", "content": call_id},
            call_id=call_id,
        )

    return FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    refused,
                    ScriptedToolCall(
                        name="math.calculate",
                        arguments={"expression": "2+2"},
                        call_id="completed-read",
                    ),
                    write("first-write"),
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(tool_calls=[write("second-write")], stop_reason=StopReason.TOOL_USE),
            ScriptedTurn(text="Both approved actions finished."),
            ScriptedTurn(text="The conversation still works."),
        ]
    )


async def exercise_refusal_recovery(
    app: Composition, *, durable: bool = False, deferred: bool = False
) -> None:
    worker = DurableWorker(
        uow_factory=app.uow_factory,
        executor=app.executor,
        clock=app.clock,
        worker_id="refusal-recovery",
    )
    run_id = await app.runs.submit("Perform the two actions after approval.")
    if durable:
        assert await worker.run_once()
    for _ in range(2):
        run = await app.runs.get(run_id)
        assert run.status is RunStatus.WAITING_FOR_APPROVAL, run.failure
        [approval] = await app.approvals.list_pending(run_id=run_id)
        await app.approvals.resolve(approval.id, ApprovalResolutionType.APPROVE_ONCE)
        if durable:
            assert await worker.run_once()
    run = await app.runs.get(run_id)
    assert run.status is RunStatus.COMPLETED, run.failure
    assert run.tool_call_count == 4
    assert run.final_message == "Both approved actions finished."
    events = await app.runs.events(run_id)
    refused = [
        event
        for event in events
        if event.event_type in {"tool.call.failed", "tool.call.denied"}
        and event.payload.get("call_id") == "refused"
    ]
    assert len(refused) == 1
    if deferred:
        assert refused[0].payload["name"] == "cockpit.check"
        assert refused[0].payload["reason_code"] == "tool.arguments_invalid"
    started = [
        event.payload["call_id"] for event in events if event.event_type == "tool.call.started"
    ]
    assert started == ["completed-read", "first-write", "second-write"]
    async with app.uow_factory() as uow:
        history = await uow.history.catch_up(run.session_id)
    validate_tool_pairs(history.items)
    results = [item for item in history.items if isinstance(item, ToolResultItem)]
    assert len(results) == 4
    assert next(item for item in results if item.call_id == "refused").is_error
    next_run_id = await app.runs.submit("Continue.", session_id=run.session_id)
    if durable:
        assert await worker.run_once()
    assert (await app.runs.get(next_run_id)).status is RunStatus.COMPLETED


@pytest.mark.parametrize(
    "refused",
    [
        ScriptedToolCall(name="math.calculate", arguments={}, call_id="refused"),
        ScriptedToolCall(name="math.calculate", arguments="{", call_id="refused"),
        ScriptedToolCall(name="unknown.tool", arguments={}, call_id="refused"),
    ],
    ids=["schema-invalid", "invalid-json", "unknown-tool"],
)
async def test_refusal_survives_two_approval_resumes(refused: ScriptedToolCall) -> None:
    async with build(settings=memory_settings(), script=refusal_script(refused)) as app:
        await exercise_refusal_recovery(app)


async def test_deferred_widget_check_refusal_survives_two_approval_resumes() -> None:
    script = refusal_script(
        ScriptedToolCall(
            name="tool.call",
            arguments={"name": "cockpit.check", "arguments": {}},
            call_id="refused",
        )
    )
    async with build(
        settings=replace(memory_settings(), cockpit_enabled=True), script=script
    ) as app:
        await exercise_refusal_recovery(app, deferred=True)
