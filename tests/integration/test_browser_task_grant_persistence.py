"""ADR-0129: PostgreSQL task grants satisfy the shared contract and the schema.

Runs only against a scratch database (DATABASE_URL); never the shared local
agent database.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from agent_core.adapters.persistence.database import create_engine, create_session_factory
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.browser import BrowserProfile, BrowserProfileStatus
from agent_core.domain.browser_task_grants import BrowserTaskGrant
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.ports.browser_task_grants import BrowserTaskGrantRepository
from tests.contract.support import NOW
from tests.contract.test_browser_task_grant_repository_contract import (
    OTHER_PROFILE_ID,
    OTHER_SESSION_ID,
    PROFILE_ID,
    SESSION_ID,
    assert_a_grant_ends_once,
    assert_concurrent_uses_stop_at_the_action_cap,
    assert_expired_grants_end_in_one_tenant,
    assert_no_use_after_expiry_revocation_or_in_another_session,
    assert_one_active_grant_per_session,
    assert_profile_grants_end_together,
    assert_task_grant_list_filters_and_pages,
    assert_task_grants_are_scoped_to_their_principal,
    assert_typed_characters_never_pass_the_grant_budget,
    grant,
)
from tests.integration.m2_support import database_settings


async def _seed(composition: Composition) -> Principal:
    owner = composition.principal
    async with composition.uow_factory() as uow:
        for session_id in (SESSION_ID, OTHER_SESSION_ID):
            await uow.sessions.create(
                Session(
                    id=session_id,
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    agent_id=UUID(int=0xA6E),
                    agent_version="1.0.0",
                    status=SessionStatus.ACTIVE,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
        for profile_id in (PROFILE_ID, OTHER_PROFILE_ID):
            await uow.browser_profiles.create(
                BrowserProfile(
                    id=profile_id,
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    provider_name="hosted-isolated",
                    provider_ref=f"opaque/{profile_id}",
                    allowed_origins=("https://www.example.org",),
                    status=BrowserProfileStatus.READY,
                    generation=4,
                    encryption_key_version="key-v1",
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
    return owner


@asynccontextmanager
async def _seeded() -> AsyncIterator[tuple[Composition, Principal]]:
    async with build(settings=database_settings(), storage="postgres") as composition:
        yield composition, await _seed(composition)


type RepositoryCase = Callable[[BrowserTaskGrantRepository, Principal], Awaitable[None]]


@pytest.mark.parametrize(
    "case",
    [
        assert_task_grants_are_scoped_to_their_principal,
        assert_one_active_grant_per_session,
        assert_task_grant_list_filters_and_pages,
        assert_typed_characters_never_pass_the_grant_budget,
        assert_no_use_after_expiry_revocation_or_in_another_session,
        assert_a_grant_ends_once,
        assert_expired_grants_end_in_one_tenant,
        assert_profile_grants_end_together,
    ],
)
async def test_postgres_task_grant_repository_satisfies_shared_contract(
    case: RepositoryCase,
) -> None:
    async with _seeded() as (composition, owner), composition.uow_factory() as uow:
        await case(uow.browser_task_grants, owner)


async def test_postgres_concurrent_uses_stop_at_the_action_cap() -> None:
    async with _seeded() as (composition, owner):
        async with composition.uow_factory() as uow:
            created = await uow.browser_task_grants.create(grant(owner=owner))

        async def consume_once() -> BrowserTaskGrant | None:
            async with composition.uow_factory() as uow:
                return await uow.browser_task_grants.consume(
                    created.id, owner, session_id=SESSION_ID, typed=0, now=NOW
                )

        async def read() -> BrowserTaskGrant:
            async with composition.uow_factory() as uow:
                return await uow.browser_task_grants.get(created.id, owner)

        await assert_concurrent_uses_stop_at_the_action_cap(consume_once, read)


def _row(owner: Principal, **overrides: Any) -> dict[str, Any]:
    return {
        "id": UUID(int=0xD1),
        "tenant_id": owner.tenant_id,
        "principal_id": owner.principal_id,
        "session_id": SESSION_ID,
        "profile_id": PROFILE_ID,
        "profile_generation": 4,
        "agent_version": "agent-v1",
        "policy_version": "policy-v1",
        "origin": "https://www.example.org",
        "path_prefix": "/lesson",
        "max_actions": 200,
        "actions_used": 0,
        "typed_characters": 0,
        "approval_id": UUID(int=0xD2),
        "approved_by": owner.principal_id,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=30),
        **overrides,
    }


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"expires_at": NOW + timedelta(minutes=31)}, "ck_browser_task_grants_time_window"),
        ({"max_actions": 201}, "ck_browser_task_grants_max_actions_bounded"),
        ({"typed_characters": 4097}, "ck_browser_task_grants_typed_characters_bounded"),
        ({"path_prefix": "/lesson/unit"}, "ck_browser_task_grants_path_prefix_segment"),
        ({"path_prefix": "lesson"}, "ck_browser_task_grants_path_prefix_segment"),
        ({"actions_used": 201, "max_actions": 200}, "ck_browser_task_grants_actions_used_bounded"),
        (
            {"end_reason": "forever", "ended_at": NOW},
            "ck_browser_task_grants_end_reason_closed",
        ),
        (
            {"revoked_at": NOW, "ended_at": NOW, "end_reason": "expired"},
            "ck_browser_task_grants_revoked_ends_revoked",
        ),
        ({"end_reason": "expired"}, "ck_browser_task_grants_end_paired"),
    ],
)
async def test_the_database_refuses_grants_outside_the_fixed_limits(
    overrides: dict[str, Any], constraint: str
) -> None:
    async with _seeded() as (_composition, owner):
        engine = create_engine(database_settings().database_url)
        try:
            async with create_session_factory(engine)() as session:
                columns = _row(owner, **overrides)
                with pytest.raises(IntegrityError) as refused:
                    await session.execute(
                        text(
                            "INSERT INTO browser_task_grants ("
                            + ", ".join(columns)
                            + ") VALUES ("
                            + ", ".join(f":{name}" for name in columns)
                            + ")"
                        ),
                        columns,
                    )
                await session.rollback()
        finally:
            await engine.dispose()

    assert constraint in str(refused.value)


async def test_task_grants_force_row_level_security_and_hold_no_material() -> None:
    engine = create_engine(database_settings().database_url)
    try:
        async with create_session_factory(engine)() as session:
            security = (
                await session.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'browser_task_grants'"
                    )
                )
            ).one()
            columns = set(
                (
                    await session.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_name = 'browser_task_grants'"
                        )
                    )
                ).scalars()
            )
    finally:
        await engine.dispose()

    assert tuple(security) == (True, True)
    assert not columns & {
        "cookies",
        "tokens",
        "storage_state",
        "credential",
        "material",
        "page_url",
        "url",
    }
