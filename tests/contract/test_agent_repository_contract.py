import pytest

from agent_core.adapters.persistence.memory import InMemoryAgentRepository
from agent_core.domain.errors import ConflictError, NotFoundError
from tests.contract.support import AGENT_ID, agent


async def test_agent_repository_pins_and_reads_versions() -> None:
    repository = InMemoryAgentRepository()
    configured = agent()
    await repository.put(configured)
    assert await repository.get_version(AGENT_ID, "1.0.0") == configured
    assert await repository.latest_version(AGENT_ID) == configured
    with pytest.raises(NotFoundError):
        await repository.get_version(AGENT_ID, "missing")

    await repository.put(configured.model_copy(deep=True))
    conflicting = configured.model_copy(update={"instructions": "Changed in place."}, deep=True)
    with pytest.raises(ConflictError, match="different content"):
        await repository.put(conflicting)
    assert await repository.get_version(AGENT_ID, "1.0.0") == configured
