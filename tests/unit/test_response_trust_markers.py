"""Input trust envelopes must not become streamed or durable reply formatting."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from uuid import UUID

import pytest

from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.bootstrap import build
from agent_core.domain.messages import (
    AssistantMessage,
    FakeModelScript,
    FileReferencePart,
    ModelCompletedEvent,
    ModelEvent,
    ModelFailedEvent,
    ModelTransientError,
    ModelTurn,
    ModelUsage,
    ProviderReasoningItem,
    ReasoningDeltaEvent,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextDeltaEvent,
    TextPart,
    ToolCallDeltaEvent,
    ToolCallItem,
    UsageEvent,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import Run, RunStatus
from agent_core.model.streaming import validated_stream
from agent_core.runtime.response_text import clean_response_text, response_text_stream
from tests.integration.m2_support import memory_settings

OPEN = '<untrusted trust="external_untrusted" source="assistant" nonce="e93c985fab44">'
CLOSE = "</untrusted:e93c985fab44>"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"Before {OPEN}evidence{CLOSE} after", "Before evidence after"),
        (f"{OPEN}\nparagraph one\n\nparagraph two\n{CLOSE}", "\nparagraph one\n\nparagraph two\n"),
        (f"{OPEN}outer {OPEN}inner{CLOSE}{CLOSE}", "outer inner"),
        (f"{CLOSE}An orphaned close", "An orphaned close"),
        (f"{OPEN}An unclosed envelope", "An unclosed envelope"),
        (f"Answer{OPEN[:-4]}", "Answer"),
        (f"Answer{CLOSE[:-4]}", "Answer"),
        (
            "<p>HTML</p> and 2 < 3; <https://example.com>",
            "<p>HTML</p> and 2 < 3; <https://example.com>",
        ),
        ("```html\n<div>example</div>\n```", "```html\n<div>example</div>\n```"),
        (
            '&lt;untrusted trust="external_untrusted"&gt;',
            '&lt;untrusted trust="external_untrusted"&gt;',
        ),
        ("<untrusted>literal</untrusted>", "<untrusted>literal</untrusted>"),
        ("comparison ends <", "comparison ends <"),
        ('<untrusted trust="not-a-trust-level"', '<untrusted trust="not-a-trust-level"'),
        ("</untrusted:not-a-nonce", "</untrusted:not-a-nonce"),
    ],
)
def test_only_internal_marker_syntax_is_removed(raw: str, expected: str) -> None:
    assert clean_response_text(raw) == expected


def _delta(text: str, index: int = 0) -> TextDeltaEvent:
    return TextDeltaEvent(
        attempt_id=UUID(int=1),
        run_id=UUID(int=2),
        step_number=1,
        sequence=0,
        item_index=index,
        text=text,
    )


async def _source(events: Sequence[ModelEvent]) -> AsyncIterator[ModelEvent]:
    for sequence, event in enumerate(events):
        yield event.model_copy(update={"sequence": sequence})


def _completed(text: str) -> ModelCompletedEvent:
    return ModelCompletedEvent(
        attempt_id=UUID(int=1),
        run_id=UUID(int=2),
        step_number=1,
        sequence=0,
        turn=ModelTurn(
            assistant_messages=[AssistantMessage(content=[TextPart(text=text)])],
            stop_reason=StopReason.END_TURN,
        ),
        stop_reason=StopReason.END_TURN,
    )


@pytest.mark.parametrize("cut", range(len(OPEN + "Evidence" + CLOSE) + 1))
async def test_every_stream_split_preserves_the_reply_without_flashing_tags(cut: int) -> None:
    raw = OPEN + "Evidence" + CLOSE
    events: list[ModelEvent] = [_delta(raw[:cut]), _delta(raw[cut:]), _completed(raw)]
    filtered = [
        event
        async for event in validated_stream(response_text_stream(validated_stream(_source(events))))
    ]
    assert (
        "".join(event.text for event in filtered if isinstance(event, TextDeltaEvent)) == "Evidence"
    )
    terminal = filtered[-1]
    assert isinstance(terminal, ModelCompletedEvent)
    assert terminal.turn.assistant_messages[0].content == [TextPart(text="Evidence")]


async def test_filters_are_per_item_and_leave_nontext_payloads_intact() -> None:
    first = _delta("Comparison <")
    second = _delta(OPEN + "reply" + CLOSE, 1)
    opaque = ProviderReasoningItem(item_index=2, provider="fake", provider_payload={"text": OPEN})
    reasoning = ReasoningDeltaEvent(
        **first.model_dump(exclude={"kind", "item_index", "text"}),
        item_index=2,
        text=OPEN,
        is_summary=True,
    )
    tool = ToolCallItem(
        item_index=3,
        call_id="call",
        name="artifact.export",
        arguments={"text": OPEN},
        raw_arguments='{"text":"keep this"}',
    )
    tool_delta = ToolCallDeltaEvent(
        **first.model_dump(exclude={"kind", "item_index", "text"}),
        item_index=3,
        call_id=tool.call_id,
        name=tool.name,
        arguments_delta=tool.raw_arguments,
    )
    usage = UsageEvent(
        **first.model_dump(exclude={"kind", "item_index", "text"}),
        usage=ModelUsage(input_tokens=13, output_tokens=21),
    )
    attachment = FileReferencePart(artifact_id=UUID(int=4), media_type="text/plain")
    turn = ModelTurn(
        assistant_messages=[
            AssistantMessage(content=[TextPart(text=first.text)]),
            AssistantMessage(item_index=1, content=[TextPart(text=second.text), attachment]),
        ],
        tool_calls=[tool],
        provider_reasoning_items=[opaque],
        usage=usage.usage,
        stop_reason=StopReason.TOOL_USE,
    )
    terminal = _completed("").model_copy(update={"turn": turn})
    events: list[ModelEvent] = [first, usage, second, reasoning, tool_delta, terminal]
    filtered = [event async for event in validated_stream(response_text_stream(_source(events)))]
    assert "".join(event.text for event in filtered if isinstance(event, TextDeltaEvent)) == (
        "Comparison <reply"
    )
    final = filtered[-1]
    assert isinstance(final, ModelCompletedEvent)
    assert final.turn.usage == turn.usage
    assert final.turn.tool_calls == [tool]
    assert final.turn.provider_reasoning_items == [opaque]
    assert final.turn.assistant_messages[0].content == [TextPart(text=first.text)]
    assert final.turn.assistant_messages[1].content == [TextPart(text="reply"), attachment]
    assert next(event for event in filtered if isinstance(event, ReasoningDeltaEvent)).text == OPEN
    assert next(event for event in filtered if isinstance(event, UsageEvent)).usage == usage.usage


async def test_partial_failure_and_character_deltas_use_the_same_filter() -> None:
    raw = OPEN + "partial reply" + CLOSE[:-4]
    completed = _completed(raw)
    terminal = ModelFailedEvent(
        **completed.model_dump(
            exclude={"kind", "turn", "stop_reason", "stop_sequence", "internal_retry_count"}
        ),
        error=ModelTransientError(
            provider="fake",
            model="scripted",
            attempt_id=UUID(int=1),
            message="provider stopped",
            stream_had_output=True,
        ),
        partial_turn=completed.turn,
    )
    filtered = [
        event async for event in response_text_stream(_source([*map(_delta, raw), terminal]))
    ]
    assert (
        "".join(event.text for event in filtered if isinstance(event, TextDeltaEvent))
        == "partial reply"
    )
    failure = filtered[-1]
    assert isinstance(failure, ModelFailedEvent)
    assert failure.error == terminal.error
    assert failure.partial_turn is not None
    assert failure.partial_turn.assistant_messages[0].content == [TextPart(text="partial reply")]


def test_a_long_invalid_candidate_does_not_swallow_following_output() -> None:
    invalid = '<untrusted trust="' + "x" * 4096
    assert clean_response_text(invalid + OPEN + "answer" + CLOSE) == invalid + "answer"


async def test_completed_only_messages_filter_across_adjacent_text_parts() -> None:
    completed = _completed("")
    completed.turn.assistant_messages[0].content = [
        TextPart(text=OPEN[:7]),
        TextPart(text=OPEN[7:] + "answer" + CLOSE),
    ]
    [filtered] = [event async for event in response_text_stream(_source([completed]))]
    assert isinstance(filtered, ModelCompletedEvent)
    assert (
        "".join(
            part.text
            for part in filtered.turn.assistant_messages[0].content
            if isinstance(part, TextPart)
        )
        == "answer"
    )


async def test_marker_only_answer_retries_instead_of_completing() -> None:
    script = FakeModelScript(
        turns=[ScriptedTurn(text=OPEN + CLOSE), ScriptedTurn(text="Recovered.")]
    )
    async with build(settings=memory_settings(), script=script) as app:
        run = await app.runs.wait_terminal(await app.runs.submit("Answer plainly."))
    assert run.status is RunStatus.COMPLETED
    assert run.final_message == "Recovered."
    assert run.model_call_count == 2


async def test_echoed_trust_markers_never_reach_live_or_saved_replies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    progress = "I'm checking affiliations."
    answer = "Some demonstrations come from people affiliated with the product."
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                text=f"{OPEN}{progress}{CLOSE}",
                tool_calls=[
                    ScriptedToolCall(name="math.calculate", arguments={"expression": "1+1"})
                ],
            ),
            ScriptedTurn(text=f"{OPEN}{answer}{CLOSE}"),
        ]
    )
    observed: list[ModelEvent] = []

    async def capture(_run: Run, event: ModelEvent) -> None:
        observed.append(event)

    async with build(settings=memory_settings(), script=script) as app:
        monkeypatch.setattr(app.executor, "_on_model_event", capture)
        run = await app.runs.wait_terminal(await app.runs.submit("Check the evidence."))
        events = await app.runs.events(run.id)
        async with app.uow_factory() as uow:
            checkpoint = await uow.checkpoints.latest(run.id)
        provider = app.executor._model_provider
        assert isinstance(provider, FakeModelProvider)
        replayed = next(
            item for item in provider.requests[1].conversation if isinstance(item, AssistantMessage)
        )

    assert run.status is RunStatus.COMPLETED, run.failure
    assert run.final_message == answer
    assert "".join(event.text for event in observed if isinstance(event, TextDeltaEvent)) == (
        progress + answer
    )
    completed = next(event for event in events if event.event_type == "assistant.message.completed")
    assert completed.payload["message"]["content"] == [{"kind": "text", "text": answer}]
    tool_turn = next(
        event
        for event in events
        if event.event_type == "model.response.completed" and event.payload["tool_names"]
    )
    assert tool_turn.payload["conversation_items"][0]["content"] == [
        {"kind": "text", "text": progress}
    ]
    assert checkpoint is not None
    replies = [item for item in checkpoint.conversation if isinstance(item, AssistantMessage)]
    assert replies
    assert all(item.trust is TrustLevel.EXTERNAL_UNTRUSTED for item in replies)
    assert replayed.trust is TrustLevel.EXTERNAL_UNTRUSTED
    assert isinstance(replayed.content[0], TextPart)
    assert replayed.content[0].text.startswith('<untrusted trust="external_untrusted"')
    assert replayed.content[0].text.count("<untrusted") == 1
    assert all(
        "<untrusted" not in part.text
        for item in replies
        for part in item.content
        if isinstance(part, TextPart)
    )
