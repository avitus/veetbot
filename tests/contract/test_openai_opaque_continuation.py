"""The real Responses adapter/runtime replays an admitted opaque collision."""

from __future__ import annotations

import json

from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.bootstrap import build
from agent_core.domain.runs import RunStatus
from tests.contract.model_fixtures import ScriptedRawSource, openai_text_events, openai_tool_events
from tests.contract.support import NOW
from tests.gates.test_runtime_tool_budget import _settings


async def test_openai_opaque_collision_survives_real_tool_loop_without_content_leakage() -> None:
    """Verify that OpenAI opaque collision survives real tool loop without content leakage."""
    opaque = "gAAAA-ciphertext-sk-" + "synthetic123456=="
    reasoning = {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "type": "reasoning",
            "id": "reasoning-item",
            "summary": [],
            "encrypted_content": opaque,
            "status": "completed",
        },
    }
    source = ScriptedRawSource(
        [[reasoning, *openai_tool_events('{"expression":"17 * 23"}')], openai_text_events("391")]
    )
    provider = OpenAIResponsesProvider(event_source=source)
    async with build(
        settings=_settings(),
        fixed_clock_at=NOW,
        sequential_ids=True,
        model_policy="balanced",
        model_provider_overrides={"openai": provider},
    ) as app:
        run_id = await app.runs.submit("Multiply 17 by 23.")
        run = await app.runs.wait_terminal(run_id)
        stored_events = await app.runs.events(run_id)
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run_id)

    assert run.status is RunStatus.COMPLETED, run.failure
    assert run.final_message == "391"
    assert len(source.requests) == 2
    [replayed] = [item for item in source.requests[1]["input"] if item.get("type") == "reasoning"]
    assert replayed["encrypted_content"] == opaque
    assert opaque not in json.dumps([event.model_dump(mode="json") for event in stored_events])
    assert opaque not in json.dumps(
        [item for item in source.requests[1]["input"] if item is not replayed]
    )
    assert checkpoint is not None and checkpoint.provider_continuation is None
