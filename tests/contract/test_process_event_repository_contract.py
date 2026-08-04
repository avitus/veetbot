from uuid import UUID

import pytest

from agent_core.adapters.persistence.memory import InMemoryProcessEventRepository
from agent_core.domain.errors import ConflictError
from agent_core.domain.events import ProcessEvent
from tests.contract.support import NOW


async def test_process_event_repository_is_append_only_and_derivation_idempotent() -> None:
    repository = InMemoryProcessEventRepository()
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
    assert await repository.list("policy.profile.loaded") == [event]
    with pytest.raises(ConflictError):
        await repository.append(event.model_copy(update={"payload": {"changed": True}}))
