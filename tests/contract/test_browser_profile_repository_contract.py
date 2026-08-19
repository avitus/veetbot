"""Shared contract for browser-profile metadata repositories."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.browser.profiles import InMemoryBrowserProfileRepository
from agent_core.domain.agents import Principal
from agent_core.domain.browser import (
    BrowserProfile,
    BrowserProfileProvisioning,
    BrowserProfileStatus,
)
from agent_core.domain.errors import ConcurrencyConflict, ConflictError, NotFoundError
from agent_core.ports.browser_profiles import BrowserProfileRepository
from tests.contract.support import NOW, principal

PROFILE_ID = UUID("00000000-0000-0000-0000-0000000000b0")


def profile(
    *,
    profile_id: UUID = PROFILE_ID,
    owner: Principal | None = None,
) -> BrowserProfile:
    owner = owner or principal()
    return BrowserProfile(
        id=profile_id,
        tenant_id=owner.tenant_id,
        principal_id=owner.principal_id,
        provider_name="isolated-hosted",
        provider_ref=f"opaque/{profile_id}",
        allowed_origins=("https://example.org",),
        status=BrowserProfileStatus.AUTHENTICATION_REQUIRED,
        generation=0,
        encryption_key_version="key-v1",
        created_at=NOW,
        updated_at=NOW,
    )


async def assert_profile_repository_scopes_create_get_and_list(
    repository: BrowserProfileRepository,
    profile_id: UUID = PROFILE_ID,
    owner: Principal | None = None,
) -> None:
    owner = owner or principal()
    expected = profile(profile_id=profile_id, owner=owner)
    created = await repository.create(expected)

    assert created == expected
    assert await repository.get(profile_id, owner) == expected
    assert expected in await repository.list(owner)

    foreign = Principal(
        tenant_id=owner.tenant_id,
        principal_id="principal-b",
        roles={"user"},
        scopes=set(),
    )
    with pytest.raises(NotFoundError):
        await repository.get(profile_id, foreign)
    assert await repository.list(foreign) == []


async def assert_profile_repository_rejects_duplicate_and_stale_writes(
    repository: BrowserProfileRepository,
    profile_id: UUID = PROFILE_ID,
    owner: Principal | None = None,
) -> None:
    owner = owner or principal()
    expected = profile(profile_id=profile_id, owner=owner)
    await repository.create(expected)

    with pytest.raises(ConflictError):
        await repository.create(expected)

    ready = await repository.transition(
        profile_id,
        owner,
        expected_generation=0,
        status=BrowserProfileStatus.READY,
        updated_at=NOW + timedelta(seconds=1),
    )
    assert ready.status is BrowserProfileStatus.READY
    assert ready.generation == 1

    with pytest.raises(ConcurrencyConflict):
        await repository.transition(
            profile_id,
            owner,
            expected_generation=0,
            status=BrowserProfileStatus.NEEDS_USER,
            updated_at=NOW + timedelta(seconds=2),
        )


async def assert_profile_repository_binds_only_the_reserved_generation(
    repository: BrowserProfileRepository,
    profile_id: UUID = PROFILE_ID,
    owner: Principal | None = None,
) -> None:
    owner = owner or principal()
    reservation = profile(profile_id=profile_id, owner=owner).model_copy(
        update={
            "provider_name": None,
            "provider_ref": None,
            "encryption_key_version": None,
            "status": BrowserProfileStatus.PROVISIONING,
        }
    )
    await repository.create(reservation)

    bound = await repository.bind(
        profile_id,
        owner,
        expected_generation=0,
        provisioning=BrowserProfileProvisioning(
            provider_name="isolated-hosted",
            provider_ref="opaque/provider-ref",
            encryption_key_version="key-v1",
        ),
        updated_at=NOW + timedelta(seconds=1),
    )

    assert bound.status is BrowserProfileStatus.AUTHENTICATION_REQUIRED
    assert bound.generation == 1
    assert bound.provider_ref == "opaque/provider-ref"
    with pytest.raises(ConcurrencyConflict):
        await repository.bind(
            profile_id,
            owner,
            expected_generation=0,
            provisioning=BrowserProfileProvisioning(
                provider_name="isolated-hosted",
                provider_ref="opaque/other",
                encryption_key_version="key-v1",
            ),
            updated_at=NOW + timedelta(seconds=2),
        )


async def assert_profile_repository_requires_revocation_before_idempotent_delete(
    repository: BrowserProfileRepository,
    profile_id: UUID = PROFILE_ID,
    owner: Principal | None = None,
) -> None:
    owner = owner or principal()
    await repository.create(profile(profile_id=profile_id, owner=owner))

    with pytest.raises(ConflictError):
        await repository.delete(profile_id, owner, expected_generation=0)

    revoked = await repository.transition(
        profile_id,
        owner,
        expected_generation=0,
        status=BrowserProfileStatus.REVOKED,
        updated_at=NOW + timedelta(seconds=1),
    )
    await repository.delete(
        profile_id,
        owner,
        expected_generation=revoked.generation,
    )
    await repository.delete(
        profile_id,
        owner,
        expected_generation=revoked.generation,
    )
    with pytest.raises(NotFoundError):
        await repository.get(profile_id, owner)


async def test_profile_repository_scopes_create_get_and_list() -> None:
    await assert_profile_repository_scopes_create_get_and_list(InMemoryBrowserProfileRepository())


async def test_profile_repository_rejects_duplicate_and_stale_writes() -> None:
    await assert_profile_repository_rejects_duplicate_and_stale_writes(
        InMemoryBrowserProfileRepository()
    )


async def test_profile_repository_binds_only_the_reserved_generation() -> None:
    await assert_profile_repository_binds_only_the_reserved_generation(
        InMemoryBrowserProfileRepository()
    )


async def test_profile_repository_requires_revocation_before_idempotent_delete() -> None:
    await assert_profile_repository_requires_revocation_before_idempotent_delete(
        InMemoryBrowserProfileRepository()
    )
