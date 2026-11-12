"""Durable projection and checkpoint recovery retain one result per refused call."""

from dataclasses import replace

from agent_core.bootstrap import build
from agent_core.domain.messages import ScriptedToolCall
from tests.integration.m2_support import database_settings
from tests.unit.test_tool_refusal_recovery import exercise_refusal_recovery, refusal_script


async def test_refusal_survives_two_durable_approval_resumes() -> None:
    script = refusal_script(
        ScriptedToolCall(
            name="tool.call",
            arguments={"name": "cockpit.check", "arguments": {}},
            call_id="refused",
        )
    )
    async with build(
        settings=replace(database_settings(), cockpit_enabled=True),
        storage="postgres",
        script=script,
    ) as app:
        await exercise_refusal_recovery(app, durable=True, deferred=True)
