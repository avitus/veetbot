"""Owner-record readings never become beliefs (Milestone 34, ``recorded_elsewhere``).

Formation reads the ``records.entry.appended`` events of a segment and
suppresses any candidate statement that carries, as standalone numeric
tokens, every component value of one appended entry.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from agent_core.domain.events import EventEnvelope
from agent_core.domain.records import canonical_value, short_value

RECORDS_APPENDED_EVENT = "records.entry.appended"
_NUMBER = re.compile(r"(?<![\d.])-?\d+(?:\.\d+)?(?!\d|\.\d)")


def recorded_value_sets(
    events: Sequence[EventEnvelope],
) -> tuple[tuple[frozenset[str], ...], ...]:
    """One tuple per appended entry; each member holds one component's two forms."""
    entries: list[tuple[frozenset[str], ...]] = []
    for event in events:
        if event.event_type != RECORDS_APPENDED_EVENT:
            continue
        values = event.payload.get("values")
        if not isinstance(values, dict) or not values:
            continue
        try:
            components = tuple(
                frozenset({canonical_value(value), short_value(value)})
                for value in (Decimal(str(text)) for text in values.values())
            )
        except (InvalidOperation, ValueError):
            continue
        entries.append(components)
    return tuple(entries)


def recorded_elsewhere(statement: str, recorded: Sequence[tuple[frozenset[str], ...]]) -> bool:
    """True when the statement carries every component of one recorded entry.

    Each component consumes its own numeric token, so an entry with two equal
    components needs the number twice.
    """
    tokens = _NUMBER.findall(statement)
    if not tokens:
        return False
    return any(entry and _covers(entry, tokens) for entry in recorded)


def _covers(entry: tuple[frozenset[str], ...], tokens: list[str]) -> bool:
    free = list(tokens)
    for forms in entry:
        match = next((index for index, token in enumerate(free) if token in forms), None)
        if match is None:
            return False
        del free[match]
    return True
