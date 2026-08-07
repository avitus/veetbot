"""Replay redacted raw provider streams through the real translators."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from agent_core.domain.messages import ModelAttempt, ModelEvent, ModelRequest, ResolvedModel
from agent_core.ports.models import ModelProvider

OPENAI_KEY_SHAPE = re.compile(r"\bsk-[a-z0-9_-]{12,}")


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

    @classmethod
    def from_path(cls, path: Path) -> RecordedFixture:
        try:
            raw: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot load recorded model fixture {path}") from exc
        return cls.model_validate(raw)


class RecordedEventSource:
    """Replay each declared raw stream once, without choosing its translator."""

    def __init__(self, fixture: RecordedFixture) -> None:
        serialized = fixture.model_dump_json()
        lowered = serialized.lower()
        if "authorization" in lowered or OPENAI_KEY_SHAPE.search(lowered) is not None:
            raise ValueError("recorded fixture contains credential-shaped content")
        self._streams = fixture.streams or [fixture.events or []]
        self._stream_index = 0

    async def __call__(self, _payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        if self._stream_index >= len(self._streams):
            raise ValueError("recorded model fixture streams are exhausted")
        events = self._streams[self._stream_index]
        self._stream_index += 1
        for event in events:
            yield dict(event)


class RecordedModelProvider:
    """A credential-free adapter fixture that still tests vendor translation."""

    name = "recorded"

    def __init__(self, delegate: ModelProvider) -> None:
        self._delegate = delegate

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
