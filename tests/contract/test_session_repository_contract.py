import pytest

from agent_core.adapters.persistence.memory import InMemorySessionRepository
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from tests.contract.support import NOW, SESSION_ID, principal, session


async def test_session_repository_scopes_reads_to_tenant_and_principal() -> None:
    repository = InMemorySessionRepository()
    await repository.create(session())
    assert await repository.get(SESSION_ID, principal()) == session()
    with pytest.raises(NotFoundError):
        await repository.get(
            SESSION_ID,
            Principal(tenant_id="other", principal_id="other", roles=set(), scopes=set()),
        )


async def test_session_close_reports_only_the_actual_transition() -> None:
    repository = InMemorySessionRepository()
    await repository.create(session())

    closed, first_transition = await repository.close(SESSION_ID, principal(), NOW)
    repeated, second_transition = await repository.close(SESSION_ID, principal(), NOW)

    assert closed == repeated
    assert first_transition is True
    assert second_transition is False
