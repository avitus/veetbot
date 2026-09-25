"""`agent run latency` measures Chat turns from the event log, without content (ADR-0131)."""

from datetime import UTC, datetime, timedelta

import pytest

from agent_core.bootstrap import Composition, build
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn
from agent_core.runtime.worker import DurableWorker
from tests.integration.m2_support import database_settings

pytestmark = pytest.mark.integration


async def _complete(app: Composition, prompt: str) -> None:
    await app.runs.submit(prompt)
    worker = DurableWorker(
        uow_factory=app.uow_factory,
        executor=app.executor,
        clock=app.clock,
        worker_id="latency-report",
    )
    assert await worker.run_once()


async def test_the_report_measures_turn_phases_models_and_tools_from_events() -> None:
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(name="math.calculate", arguments={"expression": "17 * 23"})
                ],
                delay_ms=800,
            ),
            ScriptedTurn(text="It is 391.", delay_ms=400),
        ]
    )
    # A worker heartbeat sleeping on a FixedClock would advance it mid-attempt, so the
    # fake provider's delays run on the real clock and durations carry a tolerance.
    async with build(settings=database_settings(), storage="postgres", script=script) as app:
        started = datetime.now(UTC)
        await _complete(app, "What is 17 multiplied by 23?")
        assert app.latency_report is not None

        report = await app.latency_report(started - timedelta(days=7))
        empty = await app.latency_report(started + timedelta(days=1))

    assert report.turns == 1
    phases = {phase.name: phase for phase in report.phases}
    assert phases["model"].count == 1
    assert phases["model"].p50 == pytest.approx(1.2, abs=0.4)
    turn, model_time = phases["turn"].p50, phases["model"].p50
    assert turn is not None and model_time is not None and turn >= model_time
    assert phases["model_calls"].p50 == 2
    [model] = report.models
    assert model.model == "fake/scripted"
    assert model.calls == 2
    assert model.first_text.count == 1
    assert model.first_text.p50 == pytest.approx(0.4, abs=0.3)
    assert [tool.name for tool in report.tools] == ["math.calculate"]
    assert report.queue and all(wait.session_kind for wait in report.queue)
    assert empty.turns == 0
    assert empty.phases == [] and empty.models == [] and empty.tools == []
