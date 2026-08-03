"""Replay redacted raw provider streams through the real translators."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from agent_core.adapters.models.anthropic_messages import AnthropicMessagesProvider
from agent_core.adapters.models.chat_completions import ChatCompletionsProvider
from agent_core.adapters.models.openai_responses import OpenAIResponsesProvider
from agent_core.domain.messages import ModelAttempt, ModelEvent, ModelRequest, ResolvedModel
from agent_core.ports.models import ModelProvider


class RecordedFixture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    provider_api: Literal["responses", "messages", "chat_completions"]
    events: list[dict[str, Any]] | None = None
    streams: list[list[dict[str, Any]]] | None = None

    @model_validator(mode="after")
    def exactly_one_event_shape(self) -> RecordedFixture:
        if (self.events is None) == (self.streams is None):
            raise ValueError("recorded fixture requires exactly one of events or streams")
        if self.streams is not None and not self.streams:
            raise ValueError("recorded fixture streams cannot be empty")
        return self


class RecordedModelProvider:
    """A credential-free adapter fixture that still tests vendor translation."""

    name = "recorded"

    def __init__(self, fixture: RecordedFixture) -> None:
        serialized = fixture.model_dump_json()
        lowered = serialized.lower()
        if "authorization" in lowered or "sk-ant-" in lowered or "sk-proj-" in lowered:
            raise ValueError("recorded fixture contains credential-shaped content")

        stream_index = 0

        async def source(_payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
            nonlocal stream_index
            streams = fixture.streams or [fixture.events or []]
            events = streams[min(stream_index, len(streams) - 1)]
            stream_index += 1
            for event in events:
                yield dict(event)

        delegate: ModelProvider
        if fixture.provider_api == "responses":
            delegate = OpenAIResponsesProvider(event_source=source)
        elif fixture.provider_api == "messages":
            delegate = AnthropicMessagesProvider(event_source=source)
        else:
            delegate = ChatCompletionsProvider(
                base_url="http://127.0.0.1:1/v1",
                event_source=source,
            )
        self._delegate = delegate

    @classmethod
    def from_path(cls, path: Path) -> RecordedModelProvider:
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load recorded model fixture {path}") from exc
        return cls(RecordedFixture.model_validate(raw))

    async def stream(
        self,
        request: ModelRequest,
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]:
        async for event in self._delegate.stream(request, resolved, attempt):
            yield event

    async def close(self) -> None:
        await self._delegate.close()
