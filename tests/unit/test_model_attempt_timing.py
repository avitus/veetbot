"""Completed model attempts carry the timing and usage model-gateway.md specifies (ADR-0131)."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from agent_core.bootstrap import Composition, build
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
from tests.integration.m2_support import memory_settings

START = datetime(2026, 9, 25, tzinfo=UTC)


async def _completed(app: Composition, session_id: UUID) -> list[dict[str, Any]]:
    async with app.uow_factory() as uow:
        events = await uow.events.list_after(session_id, 0, app.principal)
    return [event.payload for event in events if event.event_type == "model.response.completed"]


async def test_a_text_attempt_records_its_duration_first_event_first_text_and_usage() -> None:
    script = FakeModelScript(turns=[ScriptedTurn(text="Hello.", delay_ms=1200)])
    async with build(settings=memory_settings(), script=script, fixed_clock_at=START) as app:
        run = await app.runs.get(await app.runs.submit("Say hello."))

        [completed] = await _completed(app, run.session_id)

    assert completed["duration_ms"] == 1200
    assert completed["time_to_first_event_ms"] == 1200
    assert completed["time_to_first_text_ms"] == 1200
    assert completed["internal_retry_count"] == 0
    assert completed["usage"]["provider"] == "fake"
    assert completed["usage"]["output_tokens"] > 0


async def test_a_tool_call_attempt_has_no_time_to_first_text() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="math.calculate", arguments={"expression": "17 * 23"})
                ],
                delay_ms=300,
            ),
            ScriptedTurn(text="391.", delay_ms=50),
        ]
    )
    async with build(settings=memory_settings(), script=script, fixed_clock_at=START) as app:
        run = await app.runs.get(await app.runs.submit("What is 17 multiplied by 23?"))

        tool_step, answer_step = await _completed(app, run.session_id)

    assert tool_step["time_to_first_event_ms"] == 300
    assert tool_step["time_to_first_text_ms"] is None
    assert tool_step["duration_ms"] == 300
    assert answer_step["time_to_first_text_ms"] == 50
