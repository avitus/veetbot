"""Shared contract for exact, tenant-scoped standing browser grants."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.browser.grants import InMemoryBrowserGrantRepository
from agent_core.domain.browser import (
    BrowserActionKind,
    BrowserGrant,
    BrowserProfile,
    BrowserProfileStatus,
)
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.ports.browser_grants import BrowserGrantRepository
from tests.contract.support import NOW, principal

GRANT_ID = UUID("00000000-0000-0000-0000-0000000000a7")
PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000a8")


def grant(*, grant_id: UUID = GRANT_ID) -> BrowserGrant:
    return BrowserGrant(
        id=grant_id,
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        profile_id=PROFILE_ID,
        profile_generation=3,
        agent_version="1.0.0+h123456789abc",
        policy_version="policy-v1",
        allowed_origins=("https://example.org",),
        action_kinds=(BrowserActionKind.CLICK,),
        element_roles=("button",),
        element_names=("Continue",),
        purpose="language-practice",
        starts_at=NOW,
        expires_at=NOW + timedelta(days=7),
        approved_by=principal().principal_id,
        created_at=NOW,
        updated_at=NOW,
    )


def ready_profile() -> BrowserProfile:
    return BrowserProfile(
        id=PROFILE_ID,
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        provider_name="hosted-isolated",
        provider_ref="opaque-provider-reference",
        allowed_origins=("https://example.org",),
        status=BrowserProfileStatus.READY,
        generation=3,
        encryption_key_version="key-v1",
        created_at=NOW,
        updated_at=NOW,
    )


async def assert_grant_repository_contract(repository: BrowserGrantRepository) -> None:
    expected = grant()
    assert await repository.create(expected) == expected
    assert await repository.get(GRANT_ID, principal()) == expected
    assert await repository.list(principal(), profile_id=PROFILE_ID) == [expected]

    foreign = principal().model_copy(update={"principal_id": "principal-b"})
    with pytest.raises(NotFoundError):
        await repository.get(GRANT_ID, foreign)
    assert await repository.list(foreign, profile_id=PROFILE_ID) == []
    with pytest.raises(ConflictError):
        await repository.create(expected)

    revoked = await repository.revoke(GRANT_ID, principal(), revoked_at=NOW + timedelta(hours=1))
    assert revoked.revoked_at == NOW + timedelta(hours=1)
    assert (
        await repository.revoke(
            GRANT_ID,
            principal(),
            revoked_at=NOW + timedelta(hours=2),
        )
        == revoked
    )
    await repository.delete(GRANT_ID, principal())
    await repository.delete(GRANT_ID, principal())


async def test_in_memory_browser_grant_repository_contract() -> None:
    await assert_grant_repository_contract(InMemoryBrowserGrantRepository())


def test_browser_grant_refuses_ambient_or_unbounded_authority() -> None:
    with pytest.raises(ValueError):
        grant().model_copy(update={"expires_at": NOW + timedelta(days=31)}).model_validate(
            grant().model_dump() | {"expires_at": NOW + timedelta(days=31)}
        )
    with pytest.raises(ValueError):
        BrowserGrant.model_validate(grant().model_dump() | {"action_kinds": []})
    with pytest.raises(ValueError):
        BrowserGrant.model_validate(
            grant().model_dump() | {"allowed_origins": ["https://example.org/path"]}
        )
