from typing import Any

import pytest

from agent_core.adapters.persistence.upcasters import EventUpcasterRegistry, EventVersionError
from agent_core.domain.events import TOOL_CALL_DENIED_PAYLOAD_VERSION

APPROVAL_DENIAL = {"name": "demo.external_write", "call_id": "c1", "reason_code": "r"}
REFUSAL = {**APPROVAL_DENIAL, "result_item": {"call_id": "c1", "content": []}}
# Every recorded historical payload, by event type and stored version, with the
# current shape it must decode to.
HISTORICAL_FIXTURES: dict[tuple[str, int], list[tuple[dict[str, Any], dict[str, Any]]]] = {
    ("session.created", 1): [({"agent_id": "one"}, {"agent_id": "one", "title": None})],
    # A version-1 approval or policy denial recorded no result item; the upcast
    # makes its absence explicit instead of inventing one (ADR-0142).
    ("tool.call.denied", 1): [
        (APPROVAL_DENIAL, {**APPROVAL_DENIAL, "result_item": None}),
        (REFUSAL, REFUSAL),
    ],
    ("tool.call.denied", 2): [(REFUSAL, REFUSAL)],
}


def test_upcaster_reaches_current_shape_and_rejects_future_versions() -> None:
    registry = EventUpcasterRegistry()
    assert registry.current_version("session.created") == 2
    assert registry.current_version("tool.call.denied") == TOOL_CALL_DENIED_PAYLOAD_VERSION
    for (event_type, version), cases in HISTORICAL_FIXTURES.items():
        current = registry.current_version(event_type)
        for stored, expected in cases:
            assert registry.upcast(event_type, version, stored) == (current, expected)
    for event_type in {event_type for event_type, _version in HISTORICAL_FIXTURES}:
        current = registry.current_version(event_type)
        assert {(event_type, version) for version in range(1, current)} <= set(HISTORICAL_FIXTURES)
        with pytest.raises(EventVersionError, match="newer"):
            registry.upcast(event_type, current + 1, {})
