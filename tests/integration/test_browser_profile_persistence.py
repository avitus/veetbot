"""PostgreSQL browser-profile metadata contracts and tenant isolation."""

from __future__ import annotations

from uuid import UUID

from agent_core.bootstrap import build
from tests.contract.test_browser_authentication_repository_contract import (
    PROFILE_ID as AUTHENTICATION_PROFILE_ID,
)
from tests.contract.test_browser_authentication_repository_contract import (
    assert_authentication_repository_contract,
)
from tests.contract.test_browser_grant_repository_contract import (
    PROFILE_ID as GRANT_PROFILE_ID,
)
from tests.contract.test_browser_grant_repository_contract import (
    assert_grant_repository_contract,
    assert_grant_repository_paginates_by_created_at_and_id,
)
from tests.contract.test_browser_profile_repository_contract import (
    assert_profile_repository_binds_only_the_reserved_generation,
    assert_profile_repository_paginates_by_created_at_and_id,
    assert_profile_repository_rejects_duplicate_and_stale_writes,
    assert_profile_repository_rejects_duplicate_provider_references,
    assert_profile_repository_requires_revocation_before_idempotent_delete,
    assert_profile_repository_scopes_create_get_and_list,
    profile,
)
from tests.integration.m2_support import database_settings


async def test_postgres_browser_profile_repository_satisfies_shared_contract() -> None:
    async with (
        build(settings=database_settings(), storage="postgres") as composition,
        composition.uow_factory() as uow,
    ):
        await assert_profile_repository_scopes_create_get_and_list(
            uow.browser_profiles,
            UUID(int=0xE1),
            composition.principal,
        )
        await assert_profile_repository_rejects_duplicate_and_stale_writes(
            uow.browser_profiles,
            UUID(int=0xE2),
            composition.principal,
        )
        await assert_profile_repository_binds_only_the_reserved_generation(
            uow.browser_profiles,
            UUID(int=0xE3),
            composition.principal,
        )
        await assert_profile_repository_rejects_duplicate_provider_references(
            uow.browser_profiles,
            UUID(int=0xE5),
            composition.principal,
        )
        await assert_profile_repository_paginates_by_created_at_and_id(
            uow.browser_profiles,
            UUID(int=0xE6),
            composition.principal,
        )
        await assert_profile_repository_requires_revocation_before_idempotent_delete(
            uow.browser_profiles,
            UUID(int=0xE4),
            composition.principal,
        )


async def test_postgres_browser_grant_repository_satisfies_shared_contract() -> None:
    async with (
        build(settings=database_settings(), storage="postgres") as composition,
        composition.uow_factory() as uow,
    ):
        await uow.browser_profiles.create(profile(profile_id=GRANT_PROFILE_ID))
        await assert_grant_repository_contract(uow.browser_grants)
        await assert_grant_repository_paginates_by_created_at_and_id(uow.browser_grants)


async def test_postgres_browser_authentication_repository_satisfies_shared_contract() -> None:
    async with (
        build(settings=database_settings(), storage="postgres") as composition,
        composition.uow_factory() as uow,
    ):
        await uow.browser_profiles.create(profile(profile_id=AUTHENTICATION_PROFILE_ID))
        await assert_authentication_repository_contract(uow.browser_authentications)
