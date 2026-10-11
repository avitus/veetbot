"""Journal only touched generated copies when an erasure joins a memory UOW."""

from collections.abc import MutableMapping
from typing import Any

from agent_core.adapters.memory.transactions import MemoryTransaction


def put_copy(
    transaction: MemoryTransaction, mapping: MutableMapping[Any, Any], key: Any, value: Any
) -> None:
    missing = object()
    previous = mapping.get(key, missing)
    if previous == value:
        return

    def undo() -> None:
        # Do not overwrite a later independent writer's replacement.
        if mapping.get(key, missing) is value:
            if previous is missing:
                mapping.pop(key, None)
            else:
                mapping[key] = previous

    transaction.remember(undo)
    mapping[key] = value


def pop_copy(
    transaction: MemoryTransaction, mapping: MutableMapping[Any, Any], key: Any, default: Any = None
) -> Any:
    if key not in mapping:
        return default
    previous = mapping[key]
    transaction.remember(lambda: mapping.setdefault(key, previous))
    return mapping.pop(key)


def replace_events(
    transaction: MemoryTransaction, repository: Any, session_id: Any, updated: list[Any]
) -> None:
    originals = {event.id: event for event in repository._events.get(session_id, [])}
    previous = {
        event.id: originals[event.id]
        for event in updated
        if event.id in originals and event != originals[event.id]
    }
    if not previous:
        return

    def undo() -> None:
        # Original messages appended by another task are independent of this
        # erasure. Restore redacted rows without dropping those new events.
        repository._events[session_id] = [
            previous.get(event.id, event) for event in repository._events.get(session_id, [])
        ]

    transaction.remember(undo)
    repository._events[session_id] = updated
