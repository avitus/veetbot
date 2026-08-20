"""PostgreSQL browser-profile metadata contracts and tenant isolation."""

from __future__ import annotations

import asyncio
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


async def test_postgres_browser_authentication_admission_is_profile_serialized() -> None:
    profile_id = UUID(int=0xE7)
    async with build(settings=database_settings(), storage="postgres") as composition:
        async with composition.uow_factory() as uow:
            await uow.browser_profiles.create(
                profile(profile_id=profile_id, owner=composition.principal)
            )

        first_acquired = asyncio.Event()
        release_first = asyncio.Event()
        second_attempted = asyncio.Event()
        second_acquired = asyncio.Event()

        async def hold_first_admission() -> None:
            async with (
                composition.uow_factory() as uow,
                uow.browser_profiles.authentication_admission(
                    profile_id,
                    composition.principal,
                ),
            ):
                first_acquired.set()
                await release_first.wait()

        async def wait_for_second_admission() -> None:
            await first_acquired.wait()
            async with composition.uow_factory() as uow:
                second_attempted.set()
                async with uow.browser_profiles.authentication_admission(
                    profile_id,
                    composition.principal,
                ):
                    second_acquired.set()

        first = asyncio.create_task(hold_first_admission())
        done, _pending = await asyncio.wait({first}, timeout=0.1)
        if done:
            await first
        await asyncio.wait_for(first_acquired.wait(), timeout=1)
        second = asyncio.create_task(wait_for_second_admission())
        try:
            await asyncio.wait_for(second_attempted.wait(), timeout=1)
            await asyncio.sleep(0.05)
            assert not second_acquired.is_set()
        finally:
            release_first.set()
            await asyncio.gather(first, second)

        assert second_acquired.is_set()
