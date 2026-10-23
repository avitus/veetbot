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
_NUMBER = re.compile(r"(?<![\d.])-?\d+(?:\.\d+)?(?![\d.])")


def recorded_value_sets(events: Sequence[EventEnvelope]) -> tuple[frozenset[str], ...]:
    """One set per appended entry; each member is one component's two forms.

    A member reads ``"<canonical>|<short>"`` (for example ``"128.000000|128"``)
    so the all-components rule can tell components apart.
    """
    sets: list[frozenset[str]] = []
    for event in events:
        if event.event_type != RECORDS_APPENDED_EVENT:
            continue
        values = event.payload.get("values")
        if not isinstance(values, dict) or not values:
            continue
        try:
            members = frozenset(
                f"{canonical_value(value)}|{short_value(value)}"
                for value in (Decimal(str(text)) for text in values.values())
            )
        except (InvalidOperation, ValueError):
            continue
        sets.append(members)
    return tuple(sets)


def recorded_elsewhere(statement: str, recorded: Sequence[frozenset[str]]) -> bool:
    """True when the statement carries every component of one recorded entry."""
    tokens = set(_NUMBER.findall(statement))
    if not tokens:
        return False
    return any(
        entry and all(not tokens.isdisjoint(member.split("|")) for member in entry)
        for entry in recorded
    )
