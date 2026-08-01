from uuid import UUID

from agent_core.adapters.determinism import SequenceIdFactory


def test_id_factory_returns_unique_uuids() -> None:
    factory = SequenceIdFactory()
    values = [factory.new_id() for _ in range(3)]
    assert all(isinstance(value, UUID) for value in values)
    assert len(set(values)) == 3
