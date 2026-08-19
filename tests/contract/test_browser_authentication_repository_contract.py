"""Shared contract for secret-free browser authentication records."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.browser.authentications import (
    InMemoryBrowserAuthenticationRepository,
)
from agent_core.domain.browser import (
    BrowserAuthenticationRecord,
    BrowserAuthenticationStatus,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.ports.browser_authentications import BrowserAuthenticationRepository
from tests.contract.support import NOW, principal

CEREMONY_ID = UUID("00000000-0000-0000-0000-0000000000b7")
PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000b8")


def authentication() -> BrowserAuthenticationRecord:
    return BrowserAuthenticationRecord(
        id=CEREMONY_ID,
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        profile_id=PROFILE_ID,
        status=BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
        expires_at=NOW + timedelta(minutes=5),
        created_at=NOW,
        updated_at=NOW,
    )


async def assert_authentication_repository_contract(
    repository: BrowserAuthenticationRepository,
) -> None:
    expected = authentication()
    assert await repository.create(expected) == expected
    assert await repository.get(CEREMONY_ID, principal()) == expected
    assert await repository.list(principal(), profile_id=PROFILE_ID) == [expected]

    foreign = principal().model_copy(update={"principal_id": "principal-b"})
    with pytest.raises(NotFoundError):
        await repository.get(CEREMONY_ID, foreign)
    assert await repository.list(foreign, profile_id=PROFILE_ID) == []
    with pytest.raises(ConflictError):
        await repository.create(expected)

    updated = await repository.transition(
        CEREMONY_ID,
        principal(),
        expected_status=BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
        status=BrowserAuthenticationStatus.NEEDS_USER,
        updated_at=NOW + timedelta(seconds=1),
    )
    assert updated.status is BrowserAuthenticationStatus.NEEDS_USER
    replay = await repository.transition(
        CEREMONY_ID,
        principal(),
        expected_status=BrowserAuthenticationStatus.NEEDS_USER,
        status=BrowserAuthenticationStatus.NEEDS_USER,
        updated_at=NOW + timedelta(seconds=1),
    )
    assert replay == updated
    with pytest.raises(ConflictError):
        await repository.transition(
            CEREMONY_ID,
            principal(),
            expected_status=BrowserAuthenticationStatus.AUTHENTICATION_REQUIRED,
            status=BrowserAuthenticationStatus.READY,
            updated_at=NOW + timedelta(seconds=2),
        )


async def test_in_memory_browser_authentication_repository_contract() -> None:
    await assert_authentication_repository_contract(InMemoryBrowserAuthenticationRepository())


def test_authentication_record_cannot_contain_launch_or_browser_material() -> None:
    assert set(BrowserAuthenticationRecord.model_fields) == {
        "id",
        "tenant_id",
        "principal_id",
        "profile_id",
        "status",
        "expires_at",
        "created_at",
        "updated_at",
    }
