from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.persistence.memory import InMemorySessionRepository
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.sessions import SessionCursor, SessionTitleSource, TitleRequest
from agent_core.ports.events import EventRepository
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


async def assert_generated_title_lifecycle(repository: SessionRepository) -> None:
    """ADR-0155: only first-message and generated titles are requested and rewritten;
    every write is guarded, principal-scoped, and leaves `updated_at` alone."""

    owner = principal()
    stranger = Principal(tenant_id="other", principal_id="other", roles=set(), scopes=set())
    chat = session().model_copy(update={"id": UUID(int=801), "title": None})
    older_chat = session().model_copy(update={"id": UUID(int=802), "title": None})
    fixed = session().model_copy(update={"id": UUID(int=803), "title": "Daily review"})
    untitled = session().model_copy(update={"id": UUID(int=804), "title": None})
    for row in (chat, older_chat, fixed, untitled):
        await repository.create(row)
    await repository.set_title_if_missing(chat.id, owner, "can you look at this")
    await repository.set_title_if_missing(older_chat.id, owner, "and this one")

    later = NOW + timedelta(seconds=5)
    assert await repository.request_title(chat.id, owner, NOW) is True
    assert await repository.request_title(chat.id, owner, later) is True
    assert await repository.request_title(older_chat.id, owner, NOW) is True
    assert await repository.request_title(fixed.id, owner, NOW) is False
    assert await repository.request_title(untitled.id, owner, NOW) is False
    assert await repository.request_title(chat.id, stranger, NOW) is False

    pending = await repository.pending_title_requests(owner, limit=10)
    assert pending == [
        TitleRequest(
            session_id=older_chat.id,
            title="and this one",
            title_source=SessionTitleSource.FIRST_MESSAGE,
            requested_at=NOW,
        ),
        TitleRequest(
            session_id=chat.id,
            title="can you look at this",
            title_source=SessionTitleSource.FIRST_MESSAGE,
            requested_at=later,
        ),
    ]
    assert len(await repository.pending_title_requests(owner, limit=1)) == 1
    assert await repository.pending_title_requests(stranger, limit=10) == []

    # A title another writer already changed is never overwritten.
    assert not await repository.write_generated_title(
        chat.id, owner, expected_title="something else", title="Trip planning"
    )
    assert not await repository.write_generated_title(
        fixed.id, owner, expected_title="Daily review", title="Trip planning"
    )
    assert not await repository.write_generated_title(
        chat.id, stranger, expected_title="can you look at this", title="Trip planning"
    )
    assert await repository.write_generated_title(
        chat.id, owner, expected_title="can you look at this", title="  Trip   planning "
    )
    stored = await repository.get(chat.id, owner)
    assert stored.title == "Trip planning"
    assert stored.updated_at == chat.updated_at
    # Keeping a first-message title adopts it as generated.
    assert await repository.write_generated_title(
        older_chat.id, owner, expected_title="and this one", title="and this one"
    )
    assert (await repository.get(fixed.id, owner)).title == "Daily review"

    # The clear answers only the request it read; a later reply survives it.
    assert not await repository.clear_title_request(chat.id, owner, requested_at=NOW)
    assert await repository.pending_title_requests(owner, limit=10) == [
        TitleRequest(
            session_id=older_chat.id,
            title="and this one",
            title_source=SessionTitleSource.GENERATED,
            requested_at=NOW,
        ),
        TitleRequest(
            session_id=chat.id,
            title="Trip planning",
            title_source=SessionTitleSource.GENERATED,
            requested_at=later,
        ),
    ]
    assert not await repository.clear_title_request(chat.id, stranger, requested_at=later)
    assert await repository.clear_title_request(chat.id, owner, requested_at=later)
    assert await repository.clear_title_request(older_chat.id, owner, requested_at=NOW)
    assert await repository.pending_title_requests(owner, limit=10) == []

    # A generated title stays eligible for the next reply.
    assert await repository.request_title(chat.id, owner, later) is True
    assert (await repository.get(chat.id, owner)).updated_at == chat.updated_at


async def test_generated_title_lifecycle() -> None:
    await assert_generated_title_lifecycle(InMemorySessionRepository())


async def assert_session_index_filters_before_pagination(repository: SessionRepository) -> None:
    for value, metadata in (
        (701, {}),
        (702, {"email_operational": True}),
        (703, {"email_operational": False}),
        (704, {"email_operational": True}),
        (705, {"email_thread_id": "draft-thread"}),
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
    all_rows = await repository.list(principal(), limit=5)
    assert len(all_rows) == 5
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


async def assert_session_index_hides_people_operational_sessions(
    repository: SessionRepository,
) -> None:
    for value, metadata in (
        (721, {"purpose": "people-management"}),
        (722, {"purpose": "people-import", "people_import_job_id": "job"}),
        (723, {"purpose": "research"}),
        (724, {"purpose": ["people-management"]}),
        (725, {}),
    ):
        await repository.create(
            session().model_copy(update={"id": UUID(int=value), "metadata": metadata})
        )
    candidates = {UUID(int=value) for value in range(721, 726)}
    visible = await repository.list(principal(), limit=20, exclude_operational=True)
    assert {row.id for row in visible} & candidates == {
        UUID(int=723),
        UUID(int=724),
        UUID(int=725),
    }
    # People audit and import sessions stay readable; only the conversation index hides them.
    assert {row.id for row in await repository.list(principal(), limit=20)} >= candidates
    audit = await repository.get(UUID(int=721), principal())
    assert audit.metadata["purpose"] == "people-management"


async def test_session_index_hides_people_operational_sessions() -> None:
    await assert_session_index_hides_people_operational_sessions(InMemorySessionRepository())


async def assert_email_chat_visibility_preserves_owner_messages(
    repository: SessionRepository, events: EventRepository
) -> None:
    from agent_core.domain.events import NewEvent

    for value, event_type, actor_type in (
        (711, "user.message.created", "principal"),
        (712, "email.discussion.opened", "principal"),
        (713, "assistant.message.completed", "application"),
        (714, "user.message.created", "application"),
    ):
        session_id = UUID(int=value)
        await repository.create(
            session().model_copy(
                update={"id": session_id, "metadata": {"email_thread_id": str(value)}}
            )
        )
        await events.append(
            NewEvent(
                session_id=session_id,
                run_id=None,
                event_type=event_type,
                actor_type=actor_type,
                actor_id=principal().principal_id,
            )
        )
    visible = await repository.list(principal(), limit=20, exclude_operational=True)
    assert {row.id for row in visible} & {UUID(int=i) for i in range(711, 715)} == {
        UUID(int=711),
        UUID(int=712),
    }
    # Direct reads still preserve draft-only sessions and all their state.
    assert (await repository.get(UUID(int=713), principal())).metadata["email_thread_id"] == "713"


async def test_email_chat_visibility_preserves_owner_messages() -> None:
    from tests.contract.support import memory_stack

    _, sessions, _, events = await memory_stack()
    await assert_email_chat_visibility_preserves_owner_messages(sessions, events)


async def assert_browser_binding_is_scoped_and_preserves_metadata(
    repository: SessionRepository,
) -> None:
    chat = session().model_copy(update={"id": UUID(int=172), "metadata": {"schedule_id": "weekly"}})
    await repository.create(chat)
    async with repository.admission(chat.id, principal()) as admitted:
        assert admitted.metadata == {"schedule_id": "weekly"}
        bound = await repository.bind_browser_profile(chat.id, principal(), UUID(int=173))
        assert bound.metadata == {"schedule_id": "weekly", "browser_profile_id": str(UUID(int=173))}
    assert await repository.get(chat.id, principal()) == bound
    stranger = principal().model_copy(update={"principal_id": "stranger"})
    with pytest.raises(NotFoundError):
        await repository.bind_browser_profile(chat.id, stranger, UUID(int=174))
    assert await repository.get(chat.id, principal()) == bound


async def test_memory_browser_binding() -> None:
    await assert_browser_binding_is_scoped_and_preserves_metadata(InMemorySessionRepository())
