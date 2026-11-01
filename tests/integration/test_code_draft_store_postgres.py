"""Code draft persistence on the real PostgreSQL adapter (ADR-0281)."""

from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text

from agent_core.adapters.persistence.database import create_engine
from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.ports.code import CodeDraftRepository
from tests.contract.support import NOW
from tests.contract.test_code_draft_repository_contract import SCENARIOS, draft, proposing
from tests.integration.disposable_database import disposable_database_url
from tests.integration.m2_support import database_settings
from tests.integration.test_cockpit_store_postgres_m35 import _alembic

PRIOR_HEAD = "e8a270b10301"


@pytest.mark.parametrize("check", SCENARIOS, ids=lambda function: function.__name__)
async def test_postgres_code_draft_store(
    check: Callable[[CodeDraftRepository, Principal], Awaitable[None]], tmp_path: Path
) -> None:
    async with build(
        settings=replace(database_settings(), artifact_root=tmp_path), storage="postgres"
    ) as composition:
        owner = composition.principal.model_copy(update={"principal_id": f"drafts-{uuid4()}"})
        async with composition.uow_factory() as uow:
            await check(uow.code_drafts, owner)


async def test_a_refused_insert_leaves_the_transaction_usable(tmp_path: Path) -> None:
    async with build(
        settings=replace(database_settings(), artifact_root=tmp_path), storage="postgres"
    ) as composition:
        owner = composition.principal
        async with composition.uow_factory() as uow:
            await uow.code_drafts.put(draft(owner), expected_version=None)
            with pytest.raises(ConflictError):
                await uow.code_drafts.put(draft(owner), expected_version=None)
            await uow.code_drafts.put(draft(owner, "second-draft"), expected_version=None)
        async with composition.uow_factory() as uow:
            assert len(await uow.code_drafts.list_open(owner, limit=10)) == 2


async def test_storage_uses_jsonb_sql_null_and_forced_tenant_isolation(tmp_path: Path) -> None:
    async with build(
        settings=replace(database_settings(), artifact_root=tmp_path), storage="postgres"
    ) as composition:
        owner = composition.principal
        async with composition.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            await uow.code_drafts.put(draft(owner, "open-draft"), expected_version=None)
            await uow.code_drafts.put(proposing(owner, "sent-draft"), expected_version=None)
            types = dict(
                (
                    await uow.session.execute(
                        text(
                            "SELECT column_name, data_type FROM information_schema.columns "
                            "WHERE table_name = 'code_drafts' "
                            "AND column_name IN ('files', 'proposal')"
                        )
                    )
                )
                .tuples()
                .all()
            )
            assert types == {"files": "jsonb", "proposal": "jsonb"}
            stored = (
                await uow.session.execute(
                    text(
                        "SELECT proposal IS NULL, next_attempt_at IS NULL, "
                        "files -> 'src/agent_core/gone.py' = 'null'::jsonb "
                        "FROM code_drafts WHERE slug = 'open-draft'"
                    )
                )
            ).one()
            assert tuple(stored) == (True, True, True)
            flags = (
                await uow.session.execute(
                    text(
                        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                        "WHERE relname = 'code_drafts'"
                    )
                )
            ).one()
            assert tuple(flags) == (True, True)
            await uow.session.execute(text("CREATE ROLE code_draft_rls_probe NOLOGIN NOSUPERUSER"))
            await uow.session.execute(text("GRANT SELECT ON code_drafts TO code_draft_rls_probe"))
            await uow.session.execute(text("SET LOCAL ROLE code_draft_rls_probe"))
            assert await uow.session.scalar(text("SELECT count(*) FROM code_drafts")) == 2
            assert [item.slug for item in await uow.code_drafts.due_proposals(NOW, limit=9)] == [
                "sent-draft"
            ]
            await uow.session.execute(
                text("SELECT set_config('agent_core.tenant_id', 'another-tenant', true)")
            )
            assert await uow.session.scalar(text("SELECT count(*) FROM code_drafts")) == 0
            assert await uow.code_drafts.due_proposals(NOW, limit=9) == []
            await uow.session.rollback()


async def test_migration_downgrades_cleanly_and_refuses_to_lose_drafts(tmp_path: Path) -> None:
    async with build(
        settings=replace(database_settings(), artifact_root=tmp_path), storage="postgres"
    ) as composition:
        owner = composition.principal
        async with composition.uow_factory() as uow:
            await uow.code_drafts.put(draft(owner), expected_version=None)
        with pytest.raises(RuntimeError, match="pending proposals must not be lost"):
            _alembic("downgrade", PRIOR_HEAD)
        async with composition.uow_factory() as uow:
            assert isinstance(uow, PostgresUnitOfWork)
            await uow.session.execute(text("DELETE FROM code_drafts"))
    _alembic("downgrade", PRIOR_HEAD)
    try:
        engine = create_engine(disposable_database_url())
        try:
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT to_regclass('code_drafts')")) is None
        finally:
            await engine.dispose()
    finally:
        _alembic("upgrade", "head")
