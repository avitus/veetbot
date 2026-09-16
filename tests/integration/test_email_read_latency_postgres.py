"""Foreground reads remain independent of mailbox scans on real PostgreSQL."""

import asyncio
from dataclasses import replace

import pytest

from agent_core.adapters.persistence.email import PostgresEmailStore
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailRecord
from agent_core.domain.errors import NotFoundError
from tests.gates.test_email_experience_m26 import seed_mail
from tests.integration.m2_support import database_settings
from tests.unit.test_email_read_latency import (
    assert_reads_continue_during_admission,
    claim_session_then_fail,
    held_discovery_email,
)


@pytest.mark.parametrize("background", ["inbox", "maintenance"])
async def test_postgres_mailbox_scan_does_not_block_thread_read(
    monkeypatch: pytest.MonkeyPatch, background: str
) -> None:
    """A held database scan cannot retain the principal's advisory mutation lock."""
    principal = Principal(
        tenant_id="local", principal_id="owner", scopes={"email.read", "mcp.gmail_read.use"}
    )
    async with build(
        settings=replace(database_settings(), email_mode_enabled=True),
        storage="postgres",
        principal=principal,
    ) as app:
        thread, draft = await seed_mail(app)
        scanning, release = asyncio.Event(), asyncio.Event()
        original = PostgresEmailStore.list

        async def held_scan(
            store: PostgresEmailStore,
            owner: Principal,
            kind: str,
            *,
            after: str | None = None,
            limit: int = 1000,
        ) -> list[EmailRecord]:
            page = await original(store, owner, kind, after=after, limit=limit)
            if kind == "thread" and not scanning.is_set():
                scanning.set()
                await release.wait()
            return page

        monkeypatch.setattr(PostgresEmailStore, "list", held_scan)
        service = app.services.email
        work = asyncio.create_task(
            service.threads(principal) if background == "inbox" else service.expire_cache(principal)
        )
        try:
            await asyncio.wait_for(scanning.wait(), timeout=2)
            result = await asyncio.wait_for(service.thread(principal, thread.id), timeout=2)
            assert result["id"] == str(thread.id)
            assert result["draft"] == draft.model_dump(mode="json")
        finally:
            release.set()
            await work


@pytest.mark.parametrize("kind", ["archive", "refresh", "exclusion", "discussion", "draft"])
async def test_postgres_session_discovery_never_holds_the_advisory_lock(kind: str) -> None:
    """Email admission cannot keep the owner's advisory lock during MCP discovery."""
    async with held_discovery_email(database_settings().database_url) as (app, held):
        await assert_reads_continue_during_admission(app, held, kind)


async def test_postgres_rollback_releases_and_forgets_a_claimed_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed admission leaves neither a session row nor its prepared transports."""
    async with held_discovery_email(database_settings().database_url) as (app, held):
        session_id, thread = await claim_session_then_fail(app, held, monkeypatch)
        async with app.uow_factory() as uow:
            with pytest.raises(NotFoundError):
                await uow.sessions.get(session_id, app.principal)
            current = await app.services.email.thread_record(uow.email, app.principal, thread.id)
        assert current.session_id is None
