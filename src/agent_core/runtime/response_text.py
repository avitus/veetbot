"""Remove echoed context-envelope syntax at the runtime's reply boundary."""

from __future__ import annotations

import re
from collections.abc import AsyncIterable, AsyncIterator

from agent_core.domain.messages import (
    AssistantMessage,
    ContentPart,
    ModelCompletedEvent,
    ModelEvent,
    ModelFailedEvent,
    ModelTurn,
    TextDeltaEvent,
    TextPart,
)
from agent_core.domain.policies import TrustLevel

_OPEN = '<untrusted trust="'
_CLOSE = "</untrusted:"
_MARKER = re.compile(
    r'<untrusted trust="(?:'
    + "|".join(re.escape(trust.value) for trust in TrustLevel)
    + r')" source="[^"<>\r\n]*" nonce="[0-9a-f]{12}">|</untrusted:[0-9a-f]{12}>'
)
# A malformed candidate cannot hold an answer indefinitely or grow without bound.
_MAX_MARKER = 1024


def _truncated_marker(text: str) -> bool:
    if text.startswith(_CLOSE):
        return re.fullmatch(r"[0-9a-f]{0,12}", text[len(_CLOSE) :]) is not None
    if not text.startswith(_OPEN):
        return False
    for trust in TrustLevel:
        prefix = f'{_OPEN}{trust.value}" source="'
        if prefix.startswith(text):
            return True
        if text.startswith(prefix):
            source, separator, suffix = text[len(prefix) :].partition('"')
            if any(char in source for char in "<>\r\n"):
                return False
            if not separator or ' nonce="'.startswith(suffix):
                return True
            return re.fullmatch(r' nonce="(?:[0-9a-f]{0,12}|[0-9a-f]{12}")', suffix) is not None
    return False


class _EnvelopeFilter:
    def __init__(self) -> None:
        self.pending = ""

    def feed(self, text: str) -> str:
        remaining = self.pending + text
        self.pending = ""
        output: list[str] = []
        while remaining:
            start = remaining.find("<")
            if start < 0:
                output.append(remaining)
                break
            output.append(remaining[:start])
            remaining = remaining[start:]
            if any(
                prefix.startswith(remaining) or remaining.startswith(prefix)
                for prefix in (_OPEN, _CLOSE)
            ):
                end = remaining.find(">", 0, _MAX_MARKER)
                if end >= 0 and _MARKER.fullmatch(remaining[: end + 1]):
                    remaining = remaining[end + 1 :]
                    continue
                if end < 0 and len(remaining) < _MAX_MARKER:
                    self.pending = remaining
                    break
            # Ordinary HTML, comparisons, escaped examples and malformed tags
            # are text. Release only '<' so a later genuine marker is still seen.
            output.append("<")
            remaining = remaining[1:]
        return "".join(output)

    def finish(self) -> str:
        pending, self.pending = self.pending, ""
        # A truncated recognized envelope header is internal syntax as well.
        # Short ambiguous prefixes such as '<' or '</un' remain ordinary text.
        return "" if _truncated_marker(pending) else pending


def clean_response_text(text: str) -> str:
    """Keep envelope contents and ordinary markup, removing only internal markers."""

    scrubber = _EnvelopeFilter()
    return scrubber.feed(text) + scrubber.finish()


def _clean_message(message: AssistantMessage) -> AssistantMessage:
    scrubber = _EnvelopeFilter()
    content: list[ContentPart] = []
    last_text: TextPart | None = None
    for part in message.content:
        if isinstance(part, TextPart):
            last_text = part.model_copy(update={"text": scrubber.feed(part.text)})
            content.append(last_text)
        else:
            if last_text is not None:
                last_text.text += scrubber.finish()
            last_text = None
            content.append(part)
    if last_text is not None:
        last_text.text += scrubber.finish()
    return message.model_copy(update={"content": content})


def _clean_turn(turn: ModelTurn) -> ModelTurn:
    return turn.model_copy(
        update={
            "assistant_messages": [_clean_message(message) for message in turn.assistant_messages]
        }
    )


async def response_text_stream(source: AsyncIterable[ModelEvent]) -> AsyncIterator[ModelEvent]:
    """Filter an already validated attempt before callbacks, history and checkpoints.

    Each text item gets a fresh buffer. Usage may arrive between its deltas;
    another item or the terminal event closes it. Re-number emitted events so
    buffering and flushing preserve the normalized stream's sequence contract.
    Tool arguments, reasoning, usage and all message trust labels are unchanged.
    """

    scrubber = _EnvelopeFilter()
    active: TextDeltaEvent | None = None
    sequence = 0
    async for event in source:
        if active is not None and (
            isinstance(event, (ModelCompletedEvent, ModelFailedEvent))
            or getattr(event, "item_index", active.item_index) != active.item_index
        ):
            tail = scrubber.finish()
            if tail:
                yield active.model_copy(update={"text": tail, "sequence": sequence})
                sequence += 1
            active = None
        if isinstance(event, TextDeltaEvent):
            active = event
            text = scrubber.feed(event.text)
            if not text:
                continue
            event = event.model_copy(update={"text": text})
        elif isinstance(event, ModelCompletedEvent):
            event = event.model_copy(update={"turn": _clean_turn(event.turn)})
        elif isinstance(event, ModelFailedEvent) and event.partial_turn is not None:
            event = event.model_copy(update={"partial_turn": _clean_turn(event.partial_turn)})
        yield event.model_copy(update={"sequence": sequence})
        sequence += 1
