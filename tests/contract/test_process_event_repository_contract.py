from uuid import UUID

import pytest

from agent_core.adapters.persistence.memory import InMemoryProcessEventRepository
from agent_core.domain.errors import ConflictError
from agent_core.domain.events import ProcessEvent
from agent_core.ports.events import ProcessEventRepository
from tests.contract.support import NOW


async def assert_process_event_repository_is_append_only_and_derivation_idempotent(
    repository: ProcessEventRepository,
) -> None:
    event = ProcessEvent(
        id=UUID(int=701),
        event_type="policy.profile.loaded",
        actor_type="contract",
        payload={"policy_version": "default@profile+hline"},
        derivation_key="policy.profile.loaded:default@profile+hline",
        created_at=NOW,
    )
    assert await repository.append(event) == event
    assert await repository.append(event) == event
    loaded = await repository.get_by_derivation(event.derivation_key)
    assert loaded == event
    assert loaded is not None
    loaded.payload["policy_version"] = "mutated"
    stored = await repository.get_by_derivation(event.derivation_key)
    assert stored is not None
    assert stored.payload["policy_version"] == "default@profile+hline"
    assert await repository.get_by_derivation("missing") is None
    matching_events = [
        stored_event
        for stored_event in await repository.list("policy.profile.loaded")
        if stored_event.derivation_key == event.derivation_key
    ]
    assert matching_events == [event]
    with pytest.raises(ConflictError):
        await repository.append(event.model_copy(update={"payload": {"changed": True}}))


async def test_process_event_repository_is_append_only_and_derivation_idempotent() -> None:
    await assert_process_event_repository_is_append_only_and_derivation_idempotent(
        InMemoryProcessEventRepository()
    )
