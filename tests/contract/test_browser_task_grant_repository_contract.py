"""Shared contract for browser task-grant repositories (ADR-0129).

The helpers take a repository, or for the concurrent case a way to run one
consume in its own unit of work, so the PostgreSQL suite runs the same
assertions against real transactions.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from uuid import UUID

import pytest

from agent_core.adapters.browser.task_grants import InMemoryBrowserTaskGrantRepository
from agent_core.domain.agents import Principal
from agent_core.domain.browser_task_grants import (
    TASK_GRANT_DURATION,
    TASK_GRANT_MAX_ACTIONS,
    BrowserTaskGrant,
    BrowserTaskGrantEndReason,
    task_grant_id_for_approval,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.ports.browser_task_grants import BrowserTaskGrantRepository
from tests.contract.support import NOW, principal

SESSION_ID = UUID("00000000-0000-0000-0000-00000000c0a1")
OTHER_SESSION_ID = UUID("00000000-0000-0000-0000-00000000c0a2")
PROFILE_ID = UUID("00000000-0000-0000-0000-00000000c0b1")
OTHER_PROFILE_ID = UUID("00000000-0000-0000-0000-00000000c0b2")

type ConsumeOnce = Callable[[], Awaitable[BrowserTaskGrant | None]]


def grant(
    approval: int = 1,
    *,
    owner: Principal | None = None,
    session_id: UUID = SESSION_ID,
    profile_id: UUID = PROFILE_ID,
    created_at: datetime = NOW,
) -> BrowserTaskGrant:
    owner = owner or principal()
    approval_id = UUID(int=0xA000 + approval)
    return BrowserTaskGrant(
        id=task_grant_id_for_approval(approval_id),
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        session_id=session_id,
        profile_id=profile_id,
        profile_generation=4,
        agent_version="agent-v1",
        policy_version="policy-v1",
        origin="https://www.example.org",
        path_prefix="/lesson",
        max_actions=TASK_GRANT_MAX_ACTIONS,
        actions_used=0,
        typed_characters=0,
        approval_id=approval_id,
        approved_by=owner.principal_id,
        created_at=created_at,
        expires_at=created_at + TASK_GRANT_DURATION,
    )


def foreign_principals(owner: Principal) -> list[Principal]:
    return [
        owner.model_copy(update={"principal_id": "principal-b"}),
        owner.model_copy(update={"tenant_id": "tenant-b"}),
    ]


async def assert_task_grants_are_scoped_to_their_principal(
    repository: BrowserTaskGrantRepository, owner: Principal | None = None
) -> None:
    owner = owner or principal()
    created = await repository.create(grant(owner=owner))

    assert await repository.get(created.id, owner) == created
    with pytest.raises(ConflictError):
        await repository.create(grant(owner=owner))
    for foreign in foreign_principals(owner):
        with pytest.raises(NotFoundError):
            await repository.get(created.id, foreign)
        assert await repository.active_for_session(SESSION_ID, foreign, now=NOW) is None
        assert await repository.list(foreign, now=NOW) == []
        assert (
            await repository.consume(created.id, foreign, session_id=SESSION_ID, typed=0, now=NOW)
            is None
        )
        with pytest.raises(NotFoundError):
            await repository.end(
                created.id, foreign, reason=BrowserTaskGrantEndReason.REVOKED, now=NOW
            )
        assert (
            await repository.end_for_profile(
                PROFILE_ID, foreign, reason=BrowserTaskGrantEndReason.PROFILE_CHANGED, now=NOW
            )
            == []
        )
    assert (await repository.get(created.id, owner)).ended_at is None


async def assert_one_active_grant_per_session(
    repository: BrowserTaskGrantRepository, owner: Principal | None = None
) -> None:
    owner = owner or principal()
    first = await repository.create(grant(1, owner=owner))

    active = await repository.active_for_session(SESSION_ID, owner, now=NOW)
    with pytest.raises(ConflictError):
        await repository.create(grant(2, owner=owner))
    elsewhere = await repository.create(grant(3, owner=owner, session_id=OTHER_SESSION_ID))
    await repository.end(
        first.id, owner, reason=BrowserTaskGrantEndReason.SUPERSEDED, now=NOW + timedelta(minutes=1)
    )
    second = await repository.create(grant(2, owner=owner, created_at=NOW + timedelta(minutes=1)))

    assert active == first
    assert await repository.active_for_session(OTHER_SESSION_ID, owner, now=NOW) == elsewhere
    assert (
        await repository.active_for_session(SESSION_ID, owner, now=NOW + timedelta(minutes=2))
        == second
    )
    assert await repository.active_for_session(SESSION_ID, owner, now=second.expires_at) is None


async def assert_task_grant_list_filters_and_pages(
    repository: BrowserTaskGrantRepository, owner: Principal | None = None
) -> None:
    owner = owner or principal()
    old = await repository.create(grant(1, owner=owner))
    await repository.end(old.id, owner, reason=BrowserTaskGrantEndReason.REVOKED, now=NOW)
    later = NOW + timedelta(minutes=1)
    current = await repository.create(grant(2, owner=owner, created_at=later))
    other = await repository.create(
        grant(3, owner=owner, session_id=OTHER_SESSION_ID, created_at=later)
    )

    everything = await repository.list(owner, now=later)
    in_session = await repository.list(owner, session_id=SESSION_ID, now=later)
    active = await repository.list(owner, session_id=SESSION_ID, active_only=True, now=later)
    first_page = await repository.list(owner, now=later, limit=2)
    second_page = await repository.list(
        owner,
        now=later,
        limit=2,
        after_created_at=first_page[-1].created_at,
        after_id=first_page[-1].id,
    )

    newest = sorted([current, other], key=lambda item: item.id.int, reverse=True)
    assert [item.id for item in everything] == [*(item.id for item in newest), old.id]
    assert [item.id for item in in_session] == [current.id, old.id]
    assert [item.id for item in active] == [current.id]
    assert [item.id for item in first_page + second_page] == [item.id for item in everything]
    assert await repository.list(owner, active_only=True, now=current.expires_at) == []
    with pytest.raises(ValueError):
        await repository.list(owner, now=later, after_created_at=later)


async def assert_concurrent_uses_stop_at_the_action_cap(
    consume_once: ConsumeOnce, read: Callable[[], Awaitable[BrowserTaskGrant]]
) -> None:
    results = await asyncio.gather(*(consume_once() for _ in range(250)))
    final = await read()

    granted = [result for result in results if result is not None]
    assert len(granted) == TASK_GRANT_MAX_ACTIONS
    assert sorted(result.actions_used for result in granted) == list(range(1, 201))
    assert (final.actions_used, final.end_reason) == (200, BrowserTaskGrantEndReason.EXHAUSTED)
    assert final.ended_at is not None
    [last] = [result for result in granted if result.actions_used == TASK_GRANT_MAX_ACTIONS]
    assert last.end_reason is BrowserTaskGrantEndReason.EXHAUSTED


async def assert_typed_characters_never_pass_the_grant_budget(
    repository: BrowserTaskGrantRepository, owner: Principal | None = None
) -> None:
    owner = owner or principal()
    created = await repository.create(grant(owner=owner))

    uses = [
        await repository.consume(created.id, owner, session_id=SESSION_ID, typed=256, now=NOW)
        for _ in range(17)
    ]
    small = await repository.consume(created.id, owner, session_id=SESSION_ID, typed=0, now=NOW)
    final = await repository.get(created.id, owner)

    assert [use is not None for use in uses] == [True] * 16 + [False]
    assert small is not None and small.typed_characters == 4096
    assert (final.typed_characters, final.actions_used, final.ended_at) == (4096, 17, None)


async def assert_no_use_after_expiry_revocation_or_in_another_session(
    repository: BrowserTaskGrantRepository, owner: Principal | None = None
) -> None:
    owner = owner or principal()
    created = await repository.create(grant(owner=owner))

    wrong_session = await repository.consume(
        created.id, owner, session_id=OTHER_SESSION_ID, typed=0, now=NOW
    )
    expired = await repository.consume(
        created.id, owner, session_id=SESSION_ID, typed=0, now=created.expires_at
    )
    await repository.end(created.id, owner, reason=BrowserTaskGrantEndReason.REVOKED, now=NOW)
    revoked = await repository.consume(created.id, owner, session_id=SESSION_ID, typed=0, now=NOW)

    assert (wrong_session, expired, revoked) == (None, None, None)
    assert (await repository.get(created.id, owner)).actions_used == 0


async def assert_a_grant_ends_once(
    repository: BrowserTaskGrantRepository, owner: Principal | None = None
) -> None:
    owner = owner or principal()
    created = await repository.create(grant(owner=owner))
    at = NOW + timedelta(minutes=3)

    first, first_transitioned = await repository.end(
        created.id, owner, reason=BrowserTaskGrantEndReason.REVOKED, now=at
    )
    second, second_transitioned = await repository.end(
        created.id, owner, reason=BrowserTaskGrantEndReason.EXPIRED, now=at + timedelta(minutes=1)
    )

    assert (first_transitioned, second_transitioned) == (True, False)
    assert (first.end_reason, first.ended_at, first.revoked_at) == (
        BrowserTaskGrantEndReason.REVOKED,
        at,
        at,
    )
    assert second == first


async def assert_expired_grants_end_in_one_tenant(
    repository: BrowserTaskGrantRepository, owner: Principal | None = None
) -> None:
    owner = owner or principal()
    expiring = await repository.create(grant(1, owner=owner))
    current = await repository.create(
        grant(2, owner=owner, session_id=OTHER_SESSION_ID, created_at=NOW + timedelta(minutes=20))
    )

    none_yet = await repository.end_expired(
        expiring.expires_at - timedelta(seconds=1), 100, tenant_id=owner.tenant_id
    )
    foreign = await repository.end_expired(expiring.expires_at, 100, tenant_id="tenant-b")
    ended = await repository.end_expired(expiring.expires_at, 100, tenant_id=owner.tenant_id)
    again = await repository.end_expired(expiring.expires_at, 100, tenant_id=owner.tenant_id)

    assert (none_yet, foreign, again) == ([], [], [])
    assert [(item.id, item.end_reason) for item in ended] == [
        (expiring.id, BrowserTaskGrantEndReason.EXPIRED)
    ]
    assert (await repository.get(current.id, owner)).ended_at is None


async def assert_profile_grants_end_together(
    repository: BrowserTaskGrantRepository, owner: Principal | None = None
) -> None:
    owner = owner or principal()
    on_profile = await repository.create(grant(1, owner=owner))
    elsewhere = await repository.create(
        grant(2, owner=owner, session_id=OTHER_SESSION_ID, profile_id=OTHER_PROFILE_ID)
    )

    ended = await repository.end_for_profile(
        PROFILE_ID, owner, reason=BrowserTaskGrantEndReason.PROFILE_CHANGED, now=NOW
    )
    again = await repository.end_for_profile(
        PROFILE_ID, owner, reason=BrowserTaskGrantEndReason.PROFILE_REVOKED, now=NOW
    )

    assert [(item.id, item.end_reason) for item in ended] == [
        (on_profile.id, BrowserTaskGrantEndReason.PROFILE_CHANGED)
    ]
    assert again == []
    assert (await repository.get(elsewhere.id, owner)).ended_at is None


async def test_task_grants_are_scoped_to_their_principal() -> None:
    await assert_task_grants_are_scoped_to_their_principal(InMemoryBrowserTaskGrantRepository())


async def test_one_active_grant_per_session() -> None:
    await assert_one_active_grant_per_session(InMemoryBrowserTaskGrantRepository())


async def test_task_grant_list_filters_and_pages() -> None:
    await assert_task_grant_list_filters_and_pages(InMemoryBrowserTaskGrantRepository())


async def test_concurrent_uses_stop_at_the_action_cap() -> None:
    repository = InMemoryBrowserTaskGrantRepository()
    created = await repository.create(grant())

    async def consume_once() -> BrowserTaskGrant | None:
        return await repository.consume(
            created.id, principal(), session_id=SESSION_ID, typed=0, now=NOW
        )

    await assert_concurrent_uses_stop_at_the_action_cap(
        consume_once, lambda: repository.get(created.id, principal())
    )


async def test_typed_characters_never_pass_the_grant_budget() -> None:
    await assert_typed_characters_never_pass_the_grant_budget(InMemoryBrowserTaskGrantRepository())


async def test_no_use_after_expiry_revocation_or_in_another_session() -> None:
    await assert_no_use_after_expiry_revocation_or_in_another_session(
        InMemoryBrowserTaskGrantRepository()
    )


async def test_a_grant_ends_once() -> None:
    await assert_a_grant_ends_once(InMemoryBrowserTaskGrantRepository())


async def test_expired_grants_end_in_one_tenant() -> None:
    await assert_expired_grants_end_in_one_tenant(InMemoryBrowserTaskGrantRepository())


async def test_profile_grants_end_together() -> None:
    await assert_profile_grants_end_together(InMemoryBrowserTaskGrantRepository())
