from uuid import UUID

import pytest

from agent_core.adapters.determinism import SequenceIdFactory


def test_id_factory_returns_unique_uuids() -> None:
    factory = SequenceIdFactory([UUID(int=1)])
    values = [factory.new_id() for _ in range(3)]
    assert all(isinstance(value, UUID) for value in values)
    assert len(set(values)) == 3
    assert values == [UUID(int=1), UUID(int=2), UUID(int=3)]


def test_id_factory_rejects_duplicate_authored_values() -> None:
    factory = SequenceIdFactory([UUID(int=4), UUID(int=4)])
    assert factory.new_id() == UUID(int=4)
    with pytest.raises(ValueError, match="must be unique"):
        factory.new_id()
