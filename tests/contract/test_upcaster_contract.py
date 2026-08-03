import pytest

from agent_core.adapters.persistence.upcasters import EventUpcasterRegistry, EventVersionError


def test_upcaster_reaches_current_shape_and_rejects_future_versions() -> None:
    registry = EventUpcasterRegistry()
    version, payload = registry.upcast("session.created", 1, {"agent_id": "one"})
    assert (version, payload) == (2, {"agent_id": "one", "title": None})
    with pytest.raises(EventVersionError, match="newer"):
        registry.upcast("session.created", 3, {})
