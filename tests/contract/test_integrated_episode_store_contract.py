"""Shared behavior contract for integrated-episode repositories."""

from __future__ import annotations

import hashlib
from uuid import UUID

import pytest

from agent_core.adapters.memory.in_memory import InMemoryIntegratedEpisodeStore
from agent_core.adapters.persistence.memory_repositories import (
    PostgresIntegratedEpisodeStore,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.memory import IntegratedEpisode
from tests.contract.support import NOW, SESSION_ID, principal


def integrated_episode(*, episode_id: int = 710) -> IntegratedEpisode:
    sources = [2, 5]
    derivation = hashlib.sha256(
        f"episode-integration@1:tenant-a:principal-a:{SESSION_ID}:{sources}".encode()
    ).hexdigest()
    return IntegratedEpisode(
        id=UUID(int=episode_id),
        tenant_id="tenant-a",
        principal_id="principal-a",
        session_id=SESSION_ID,
        source_event_ids=sources,
        source_started_at=NOW,
        source_ended_at=NOW,
        narrative="[e:2] User is building an agent. [e:5] User is comparing web tools.",
        subjects=["personal AI agent", "web tools"],
        integration_policy_version="episode-integration@1",
        derivation_key=derivation,
        created_at=NOW,
    )


async def test_integrated_episode_repository_contract_is_registered() -> None:
    """The executable contract covers idempotency, scope, paging, and erasure."""

    required = {"put", "get", "for_session", "delete_for_session", "delete_for_principal"}
    for repository_type in (InMemoryIntegratedEpisodeStore, PostgresIntegratedEpisodeStore):
        assert required <= set(dir(repository_type))

    store = InMemoryIntegratedEpisodeStore()
    episode = integrated_episode()
    assert await store.put(episode) == episode
    assert await store.put(episode) == episode
    conflicting = episode.model_copy(update={"derivation_key": "f" * 64})
    with pytest.raises(ConflictError, match="episode id identifies different content"):
        await store.put(conflicting)
    assert await store.get(episode.id, principal()) == episode
    assert await store.for_session(SESSION_ID, principal()) == [episode]

    foreign = principal().model_copy(update={"principal_id": "other"})
    with pytest.raises(NotFoundError):
        await store.get(episode.id, foreign)
    assert await store.for_session(SESSION_ID, foreign) == []
    assert await store.delete_for_session(SESSION_ID, foreign) == 0
    assert await store.delete_for_session(SESSION_ID, principal()) == 1
    with pytest.raises(NotFoundError):
        await store.get(episode.id, principal())

    owned = integrated_episode(episode_id=711)
    assert await store.put(owned) == owned
    assert await store.delete_for_principal(principal()) == 1
    with pytest.raises(NotFoundError):
        await store.get(owned.id, principal())


async def test_in_memory_episode_derivation_keys_are_owner_scoped() -> None:
    store = InMemoryIntegratedEpisodeStore()
    first = integrated_episode()
    other_principal = principal().model_copy(update={"principal_id": "other"})
    second = first.model_copy(
        update={
            "id": UUID(int=712),
            "principal_id": other_principal.principal_id,
        }
    )

    assert await store.put(first) == first
    assert await store.put(second) == second
    assert await store.get(first.id, principal()) == first
    assert await store.get(second.id, other_principal) == second
