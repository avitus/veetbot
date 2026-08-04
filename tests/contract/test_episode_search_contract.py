"""Episodic search interface contract."""

import inspect

from agent_core.memory.retrieval import EventEpisodeSearch


def test_episode_search_is_explicit_and_async() -> None:
    assert inspect.iscoroutinefunction(EventEpisodeSearch.search)
