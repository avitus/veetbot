"""Episodic search contract: isolation, time windows, text, and limits."""

from datetime import timedelta

import pytest

from agent_core.adapters.persistence.memory import InMemoryEventRepository
from agent_core.domain.memory import EpisodeQuery
from agent_core.memory.retrieval import EPISODE_PAGE_MINIMUM, EventEpisodeSearch
from tests.contract.memory_fixtures import formation_stack, user_event
from tests.contract.support import PRINCIPAL_ID, SESSION_ID, TENANT, principal


async def test_episode_search_scopes_isolation_time_text_and_limit() -> None:
    clock, factory, _service, _retriever = await formation_stack()
    episodes = EventEpisodeSearch(factory, principal())
    first = await user_event(factory, "alpha deploy discussion")
    clock.advance(timedelta(minutes=1))
    cut = clock.now()
    second = await user_event(factory, "beta deploy discussion")
    clock.advance(timedelta(minutes=1))
    third = await user_event(factory, "gamma rollback note")

    query = EpisodeQuery(tenant_id=TENANT, principal_id=PRINCIPAL_ID, session_id=SESSION_ID)

    foreign = await episodes.search(query.model_copy(update={"principal_id": "principal-b"}))
    assert foreign == []

    everything = await episodes.search(query)
    assert [event.sequence for event in everything] == [first, second, third]

    since = await episodes.search(query.model_copy(update={"since": cut}))
    assert [event.sequence for event in since] == [second, third]

    until = await episodes.search(query.model_copy(update={"until": cut}))
    assert [event.sequence for event in until] == [first]

    limited = await episodes.search(query.model_copy(update={"limit": 1}))
    assert [event.sequence for event in limited] == [first]

    matched = await episodes.search(query.model_copy(update={"text": "DEPLOY"}))
    assert [event.sequence for event in matched] == [first, second]

    limited_match = await episodes.search(query.model_copy(update={"text": "deploy", "limit": 1}))
    assert [event.sequence for event in limited_match] == [first]


async def test_episode_search_pages_bounded_reads_until_the_limit_is_met(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search pages the stream: every read is bounded, and later pages are read.

    A match past the first page must still be found, so the search cannot stop
    at one page, and no read may ask the event store for the whole session.
    """

    _clock, factory, _service, _retriever = await formation_stack()
    episodes = EventEpisodeSearch(factory, principal())
    pages: list[int | None] = []
    original = InMemoryEventRepository.list_after

    async def _recording_list_after(
        self: InMemoryEventRepository, *args: object, **kwargs: object
    ) -> object:
        pages.append(kwargs.get("limit"))  # type: ignore[arg-type]
        return await original(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(InMemoryEventRepository, "list_after", _recording_list_after)

    for index in range(EPISODE_PAGE_MINIMUM + 4):
        await user_event(factory, f"routine note {index}")
    needle = await user_event(factory, "the archived kestrel decision")
    pages.clear()

    query = EpisodeQuery(
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        session_id=SESSION_ID,
        text="kestrel",
        limit=1,
    )

    found = await episodes.search(query)

    assert [event.sequence for event in found] == [needle]
    assert pages == [EPISODE_PAGE_MINIMUM, EPISODE_PAGE_MINIMUM]

    pages.clear()
    bounded = await episodes.search(query.model_copy(update={"text": "routine", "limit": 3}))
    assert len(bounded) == 3
    assert pages == [EPISODE_PAGE_MINIMUM]
