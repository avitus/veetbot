from uuid import UUID

import pytest

from agent_core.adapters.persistence.memory import InMemorySessionRepository
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.sessions import SessionCursor
from agent_core.ports.repositories import SessionRepository
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


async def test_session_title_is_first_writer_wins_and_principal_scoped() -> None:
    repository = InMemorySessionRepository()
    await repository.create(session())

    titled = await repository.set_title_if_missing(SESSION_ID, principal(), "First title")
    repeated = await repository.set_title_if_missing(SESSION_ID, principal(), "Second title")

    assert titled.title == repeated.title == "First title"
    with pytest.raises(NotFoundError):
        await repository.set_title_if_missing(
            SESSION_ID,
            Principal(tenant_id="other", principal_id="other", roles=set(), scopes=set()),
            "Leaked title",
        )


async def test_session_title_is_normalized_and_bounded() -> None:
    repository = InMemorySessionRepository()
    await repository.create(session())

    titled = await repository.set_title_if_missing(
        SESSION_ID, principal(), "  A   title\n" + "x" * 100
    )

    assert titled.title is not None
    assert titled.title.startswith("A title ")
    assert len(titled.title) == 64


async def assert_session_index_filters_before_pagination(repository: SessionRepository) -> None:
    for value, metadata in (
        (701, {}),
        (702, {"email_operational": True}),
        (703, {"email_operational": False}),
        (704, {"email_operational": True}),
    ):
        await repository.create(
            session().model_copy(update={"id": UUID(int=value), "metadata": metadata})
        )
    first = await repository.list(principal(), limit=1, exclude_operational=True)
    assert [row.id for row in first] == [UUID(int=703)]
    second = await repository.list(
        principal(),
        limit=1,
        cursor=SessionCursor(updated_at=first[0].updated_at, id=first[0].id),
        exclude_operational=True,
    )
    assert [row.id for row in second] == [UUID(int=701)]
    all_rows = await repository.list(principal(), limit=4)
    assert len(all_rows) == 4
    assert (
        await repository.list(
            principal().model_copy(update={"principal_id": "foreign"}),
            limit=4,
            exclude_operational=True,
        )
        == []
    )


async def test_session_index_filters_before_pagination() -> None:
    await assert_session_index_filters_before_pagination(InMemorySessionRepository())
