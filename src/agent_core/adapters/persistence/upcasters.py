"""Pure, total event-payload upcasting on the durable read path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class EventVersionError(ValueError):
    """Raised when an event payload cannot be decoded by this code revision."""


@dataclass(frozen=True, slots=True)
class SessionCreatedV1ToV2:
    event_type: str = "session.created"
    from_version: int = 1
    to_version: int = 2

    def upcast(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {**payload, "title": None}


class EventUpcasterRegistry:
    """Chain immutable payload copies to each event type's current version."""

    def __init__(self) -> None:
        authored = [SessionCreatedV1ToV2()]
        self._upcasters = {(item.event_type, item.from_version): item for item in authored}
        self._current_versions = {
            event_type: max(item.to_version for item in authored if item.event_type == event_type)
            for event_type in {item.event_type for item in authored}
        }

    def current_version(self, event_type: str) -> int:
        return self._current_versions.get(event_type, 1)

    def upcast(
        self, event_type: str, stored_version: int, payload: dict[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        current = self.current_version(event_type)
        if stored_version > current:
            raise EventVersionError(
                f"event {event_type!r} payload version {stored_version} is newer than {current}"
            )
        version = stored_version
        result = dict(payload)
        while version < current:
            upcaster = self._upcasters.get((event_type, version))
            if upcaster is None or upcaster.to_version != version + 1:
                raise EventVersionError(
                    f"event {event_type!r} has no total upcast from version {version}"
                )
            result = upcaster.upcast(dict(result))
            version = upcaster.to_version
        return version, result
