"""Opt-in regressions over owner-private captured model inputs, never committed data."""

import json
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


@pytest.mark.parametrize(
    "case",
    [
        "garden-explicit",
        "training-implicit",
        "portfolio-implicit",
        "mother-implicit",
        "mother-planning",
    ],
)
async def test_recall_planning_and_answer_use(case: str) -> None:
    live_enabled()
    root = os.environ.get("RECALL_AUDIT_ROOT")
    key = os.environ.get("VEETBOT_OPENAI_KEY")
    if not root or not key:
        pytest.skip("Requires RECALL_AUDIT_ROOT and VEETBOT_OPENAI_KEY")
    capture_case = "mother-implicit" if case == "mother-planning" else case
    captures = sorted(
        (Path(root) / "requests" / capture_case).glob("*.json"),
        key=lambda path: path.stat().st_mtime,
    )
    assert captures, "The requested audit case must have captured model inputs"
    answer_case = case in {"training-implicit", "mother-implicit"}
    source = captures[-1] if answer_case else captures[0]
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
    if answer_case:
        request.tool_choice = "none"
    resolved = await router().resolve("astra", tenant_id="live")
    provider = OpenAIResponsesProvider(api_key=key)
    try:
        turn = await collect_turn(provider.stream(request, resolved, attempt()))
    finally:
        await provider.close()
    if case == "portfolio-implicit":
        assert any(call.name == "memory.search" for call in turn.tool_calls), (
            "A historical list must not be presented as the latest complete set "
            "without checking updates"
        )
        assert any(
            call.name == "memory.search"
            and re.search(r"\b(my|our)\b", call.arguments.get("text", ""), re.I)
            for call in turn.tool_calls
        ), "Owner collection queries must preserve first-person ownership"
        criteria = json.loads((Path(root) / "replay-criteria.json").read_text())[case]
        assert criteria["known_list_members"] and all(
            isinstance(member, str) and member.strip() for member in criteria["known_list_members"]
        ), "The private fixture must name the already-known list members"
        search_text = " ".join(
            call.arguments.get("text", "")
            for call in turn.tool_calls
            if call.name == "memory.search"
        ).casefold()
        assert not any(
            member.casefold() in search_text for member in criteria["known_list_members"]
        ), "Search the missing changes instead of repeating already-known list members"
        assert not re.search(r"\b(briefing|coverage|checklist)\b", search_text), (
            "Search underlying collection changes, not instructions for the requested artifact"
        )
    elif case == "mother-planning":
        criteria = json.loads((Path(root) / "replay-criteria.json").read_text())[capture_case]
        name = criteria["person_name"]
        assert isinstance(name, str) and name.strip(), "The fixture must name the known person"
        assert any(
            call.name == "memory.search"
            and name.casefold() in call.arguments.get("text", "").casefold()
            and re.search(
                r"\b(residence|community|facility|living)\b",
                call.arguments.get("text", ""),
                re.I,
            )
            for call in turn.tool_calls
        ), "Disambiguate another person's missing baseline using their grounded name"
    elif case == "garden-explicit":
        searches = [call.arguments.get("text", "") for call in turn.tool_calls]
        assert any(re.search(r"\b(home|residence|address)\b", text, re.I) for text in searches), (
            f"Location-dependent planning must search the missing residence fact: {searches!r}"
        )
        assert any(
            re.search(r"\b(my|our)\b", text, re.I)
            and re.search(r"\b(home|residence|address)\b", text, re.I)
            for text in searches
        ), "Owner residence queries must preserve first-person ownership, not resolve a namesake"
    else:
        answer = " ".join(
            part.text
            for message in turn.assistant_messages
            for part in message.content
            if isinstance(part, TextPart)
        )
        if case == "mother-implicit":
            criteria = json.loads((Path(root) / "replay-criteria.json").read_text())[case]
            assert criteria["baseline_terms"] and all(
                isinstance(term, str) and term.strip() for term in criteria["baseline_terms"]
            ), "The private fixture must name the current comparison baseline"
            assert any(
                term.casefold() in answer.casefold() for term in criteria["baseline_terms"]
            ), "Identify the known current baseline when proposing a comparable replacement"
        else:
            assert re.search(r"\bswim\w*\b", answer, re.I), (
                "Adapt the known swimming routine explicitly instead of generic cardio"
            )
