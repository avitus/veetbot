"""Episodic search contract: isolation, time windows, text, and limits."""

from datetime import timedelta

from agent_core.domain.memory import EpisodeQuery
from agent_core.memory.retrieval import EventEpisodeSearch
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
