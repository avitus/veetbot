"""Opt-in regressions over owner-private captured model inputs, never committed data."""

import os
import re
from pathlib import Path

import pytest

from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.bootstrap import DEFAULT_AGENT_INSTRUCTIONS
from agent_core.domain.messages import ModelRequest, TextPart
from agent_core.domain.tools import ToolSpec
from agent_core.model.streaming import collect_turn
from tests.live.test_model_providers_m3 import attempt, live_enabled, router


@pytest.mark.parametrize("case", ["garden-explicit", "training-implicit"])
async def test_recall_planning_and_answer_use(case: str) -> None:
    live_enabled()
    root = os.environ.get("RECALL_AUDIT_ROOT")
    key = os.environ.get("VEETBOT_OPENAI_KEY")
    if not root or not key:
        pytest.skip("Requires RECALL_AUDIT_ROOT and VEETBOT_OPENAI_KEY")
    captures = sorted(
        (Path(root) / "requests" / case).glob("*.json"), key=lambda path: path.stat().st_mtime
    )
    assert captures, "The requested audit case must have captured model inputs"
    source = captures[0] if case == "garden-explicit" else captures[-1]
    request = ModelRequest.model_validate_json(source.read_text())
    request.tools = [ToolSpec.model_validate(tool) for tool in request.tools]
    for item in request.conversation:
        if item.kind == "system" and any(
            isinstance(part, TextPart) and part.text.startswith("Answer the user's request")
            for part in item.content
        ):
            item.content = [TextPart(text=DEFAULT_AGENT_INSTRUCTIONS)]
            break
    else:
        pytest.fail("Captured agent instructions are missing")
    if case == "training-implicit":
        request.tool_choice = "none"
    resolved = await router().resolve("astra", tenant_id="live")
    provider = OpenAIResponsesProvider(api_key=key)
    try:
        turn = await collect_turn(provider.stream(request, resolved, attempt()))
    finally:
        await provider.close()
    if case == "garden-explicit":
        searches = [call.arguments.get("text", "") for call in turn.tool_calls]
        assert any(re.search(r"\b(home|residence|address)\b", text, re.I) for text in searches), (
            "Location-dependent planning must search the missing residence fact"
        )
    else:
        answer = " ".join(
            part.text
            for message in turn.assistant_messages
            for part in message.content
            if isinstance(part, TextPart)
        )
        assert re.search(r"\bswim\w*\b", answer, re.I), (
            "Adapt the known swimming routine explicitly instead of generic cardio"
        )
