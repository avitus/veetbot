"""Foreground reads remain independent of mailbox scans on real PostgreSQL."""

import asyncio
from dataclasses import replace

import pytest

from agent_core.adapters.persistence.email import PostgresEmailStore
from agent_core.bootstrap import build
from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailRecord
from tests.gates.test_email_experience_m26 import seed_mail
from tests.integration.m2_support import database_settings


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
