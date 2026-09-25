"""Reading one conversation must not wait for mailbox-wide housekeeping."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from agent_core.adapters.mcp.scripted import ScriptedMCPClient
from agent_core.adapters.persistence.email import InMemoryEmailStore
from agent_core.bootstrap import Composition, build
from agent_core.domain.agents import Principal
from agent_core.domain.credentials import SecretValue
from agent_core.domain.email import EmailDraftStatus, EmailRecord, EmailThread
from agent_core.domain.errors import ConflictError
from agent_core.domain.mcp import MCPDiscovery, MCPServerConfig
from agent_core.domain.sessions import Session
from tests.gates.test_email_experience_m26 import email_client, seed_mail
from tests.gates.test_email_m18 import _email_settings
from tests.gates.test_email_runtime_m26 import _current_mail_factory, _seed_draft


@pytest.mark.parametrize("resource", ["thread", "draft", "draft_revisions"])
async def test_detail_reads_do_not_scan_unrelated_mail(
    monkeypatch: pytest.MonkeyPatch, resource: str
) -> None:
    """Even expired mail elsewhere cannot turn a point read into global cleanup."""
    async with email_client() as (app, client):
        thread, draft = await seed_mail(app)
        other, _ = await seed_mail(app)
        async with app.uow_factory() as uow:
            row = await uow.email.get(app.principal, "thread", str(other.id))
            assert row is not None
            old = other.model_copy(
                update={"last_accessed_at": app.clock.now() - timedelta(days=31)}
            )
            await uow.email.put(
                row.model_copy(update={"revision": 2, "payload": old.model_dump(mode="json")}),
                expected_revision=1,
            )
        scanned: list[str] = []
        original = InMemoryEmailStore.list

        async def record_scan(
            store: InMemoryEmailStore,
            principal: Principal,
            kind: str,
            *,
            after: str | None = None,
            limit: int = 1000,
        ) -> list[EmailRecord]:
            scanned.append(kind)
            return await original(store, principal, kind, after=after, limit=limit)

        monkeypatch.setattr(InMemoryEmailStore, "list", record_scan)
        path = {
            "thread": f"threads/{thread.id}",
            "draft": f"drafts/{draft.id}",
            "draft_revisions": f"drafts/{draft.id}/revisions",
        }[resource]
        response = await client.get(f"/v1/email/{path}")
        assert response.status_code == 200
        assert "thread" not in scanned and "draft" not in scanned
        if resource == "thread":
            assert response.json()["messages"][0]["body"] == thread.messages[0].body
            assert response.json()["draft"]["body"] == draft.body
        async with app.uow_factory() as uow:
            unchanged = await uow.email.get(app.principal, "thread", str(other.id))
        assert unchanged is not None and unchanged.revision == 2


async def test_point_reads_still_enforce_body_and_draft_retention() -> None:
    """Removing global cleanup must never return or renew expired bodies."""
    async with email_client() as (app, client):
        thread, draft = await seed_mail(app)
        old = app.clock.now() - timedelta(days=31)
        async with app.uow_factory() as uow:
            for kind, value in (
                ("thread", thread.model_copy(update={"last_accessed_at": old})),
                (
                    "draft",
                    draft.model_copy(update={"status": EmailDraftStatus.SENT, "updated_at": old}),
                ),
            ):
                row = await uow.email.get(app.principal, kind, str(value.id))
                assert row is not None
                await uow.email.put(
                    row.model_copy(
                        update={"revision": 2, "payload": value.model_dump(mode="json")}
                    ),
                    expected_revision=1,
                )
        response = await client.get(f"/v1/email/threads/{thread.id}")
        assert response.status_code == 200
        assert response.json()["messages"][0]["body"] == ""
        assert response.json()["complete"] is False
        assert response.json()["draft"]["body"] == ""
        assert (await client.get(f"/v1/email/drafts/{draft.id}")).json()["body"] == ""
        assert (await client.get(f"/v1/email/drafts/{draft.id}/revisions")).json()["items"] == []


@pytest.mark.parametrize("background", ["inbox", "maintenance"])
async def test_mailbox_scan_does_not_hold_up_selected_thread(
    monkeypatch: pytest.MonkeyPatch, background: str
) -> None:
    """A deliberately held scan must not retain the owner's email mutation lock."""
    async with email_client() as (app, _):
        thread, _ = await seed_mail(app)
        scanning, release = asyncio.Event(), asyncio.Event()
        original = InMemoryEmailStore.list

        async def delayed_scan(
            store: InMemoryEmailStore,
            principal: Principal,
            kind: str,
            *,
            after: str | None = None,
            limit: int = 1000,
        ) -> list[EmailRecord]:
            if kind == "thread" and not scanning.is_set():
                scanning.set()
                await release.wait()
            return await original(store, principal, kind, after=after, limit=limit)

        monkeypatch.setattr(InMemoryEmailStore, "list", delayed_scan)
        service = app.services.email
        work = asyncio.create_task(
            service.threads(app.principal)
            if background == "inbox"
            else service.expire_cache(app.principal)
        )
        try:
            await asyncio.wait_for(scanning.wait(), timeout=1)
            result = await asyncio.wait_for(service.thread(app.principal, thread.id), timeout=1)
            assert result["id"] == str(thread.id)
        finally:
            release.set()
            await work


async def test_maintenance_rechecks_a_refreshed_candidate_before_erasing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A source refreshed after discovery must survive a stale cleanup candidate."""
    async with email_client() as (app, _):
        thread, _ = await seed_mail(app)
        async with app.uow_factory() as uow:
            row = await uow.email.get(app.principal, "thread", str(thread.id))
            assert row is not None
            old = thread.model_copy(
                update={"last_accessed_at": app.clock.now() - timedelta(days=31)}
            )
            await uow.email.put(
                row.model_copy(update={"revision": 2, "payload": old.model_dump(mode="json")}),
                expected_revision=1,
            )
        original = InMemoryEmailStore.list

        async def refresh_after_snapshot(
            store: InMemoryEmailStore,
            principal: Principal,
            kind: str,
            *,
            after: str | None = None,
            limit: int = 1000,
        ) -> list[EmailRecord]:
            page = await original(store, principal, kind, after=after, limit=limit)
            if kind == "thread" and page:
                refreshed = thread.model_copy(update={"last_accessed_at": app.clock.now()})
                await store.put(
                    page[0].model_copy(
                        update={"revision": 3, "payload": refreshed.model_dump(mode="json")}
                    ),
                    expected_revision=2,
                )
            return page

        monkeypatch.setattr(InMemoryEmailStore, "list", refresh_after_snapshot)
        assert await app.services.email.expire_cache(app.principal) == 0
        result = await app.services.email.thread(app.principal, thread.id)
        assert result["messages"] == [
            message.model_dump(mode="json") for message in thread.messages
        ]


@dataclass
class HeldDiscovery:
    """MCP discovery that the test can hold open, as a slow stdio server does."""

    release: asyncio.Event = field(default_factory=asyncio.Event)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    closed: list[UUID] = field(default_factory=list)
    pending: int | None = 0

    def hold(self, calls: int | None = None) -> None:
        """Hold the next ``calls`` discoveries, or all of them, until released."""
        self.pending = calls
        self.closed.clear()
        self.started.clear()
        self.release.clear()

    def take(self) -> bool:
        if self.release.is_set() or self.pending == 0:
            return False
        if self.pending is not None:
            self.pending -= 1
        return True


@asynccontextmanager
async def held_discovery_email(
    database_url: str | None = None,
) -> AsyncIterator[tuple[Composition, HeldDiscovery]]:
    """Compose Email whose admitted runs stay queued, like the PostgreSQL dispatcher."""
    base = await _current_mail_factory()
    held = HeldDiscovery()

    def factory(
        config: MCPServerConfig, credential: SecretValue | None, environment: dict[str, str]
    ) -> ScriptedMCPClient:
        client = base(config, credential, environment)
        discover = client.discover

        async def held_discover() -> MCPDiscovery:
            if held.take():
                held.started.set()
                await held.release.wait()
            return await discover()

        vars(client)["discover"] = held_discover
        return client

    settings = replace(_email_settings(), email_mode_enabled=True)
    async with build(
        settings=settings if database_url is None else replace(settings, database_url=database_url),
        storage="memory" if database_url is None else "postgres",
        mcp_client_factory=factory,
    ) as app:
        service = app.services.email

        async def queued(run_id: UUID) -> None:
            return None

        close = service.close_session
        assert close is not None

        async def record_close(session_id: UUID) -> None:
            held.closed.append(session_id)
            await close(session_id)

        service.dispatch = queued
        service.close_session = record_close
        service.release_session_transports = record_close
        yield app, held


async def _unbind_thread_session(app: Composition, thread: EmailThread) -> EmailThread:
    async with app.uow_factory() as uow:
        row = await uow.email.get(app.principal, "thread", str(thread.id))
        assert row is not None
        unbound = EmailThread.model_validate(row.payload).model_copy(update={"session_id": None})
        await uow.email.put(
            row.model_copy(
                update={"revision": row.revision + 1, "payload": unbound.model_dump(mode="json")}
            ),
            expected_revision=row.revision,
        )
    return unbound


async def _next_thread(app: Composition, thread: EmailThread) -> EmailThread:
    """Store the conversation the owner opens after acting on ``thread``."""
    following = thread.model_copy(
        update={
            "id": uuid4(),
            "provider_thread_id": "thread-2",
            "session_id": None,
            "draft_id": None,
            "archive_operation": None,
        }
    )
    async with app.uow_factory() as uow:
        await uow.email.put(
            EmailRecord(
                tenant_id=app.principal.tenant_id,
                principal_id=app.principal.principal_id,
                kind="thread",
                key=str(following.id),
                revision=1,
                payload=following.model_dump(mode="json"),
                created_at=app.clock.now(),
                updated_at=app.clock.now(),
            ),
            expected_revision=0,
        )
    return following


async def _thread_sessions(app: Composition, thread: EmailThread) -> set[UUID]:
    async with app.uow_factory() as uow:
        return {
            row.id
            for row in await uow.sessions.list(app.principal, limit=100)
            if row.metadata.get("email_thread_id") == str(thread.id)
        }


async def _session_pins(app: Composition, session_id: UUID) -> list[object]:
    async with app.uow_factory() as uow:
        created = await uow.events.latest_before(
            session_id, (1 << 63) - 1, "session.created", app.principal
        )
    assert created is not None
    pins = created.payload["skill_pins"]
    assert isinstance(pins, list)
    return pins


async def _admit(app: Composition, kind: str, thread: EmailThread) -> UUID:
    """Run one email command that may create a session; return that session."""
    service = app.services.email
    principal = app.principal
    if kind in {"archive", "refresh", "draft"}:
        operation = await service.submit_task(
            principal,
            kind=kind,  # type: ignore[arg-type]
            thread_id=None if kind == "refresh" else thread.id,
            expected_revision=None if kind == "refresh" else thread.revision,
            archived=True if kind == "archive" else None,
            idempotency_key=f"latency-{kind}",
        )
        async with app.uow_factory() as uow:
            return (await uow.runs.get(operation.run_id, principal)).session_id
    if kind == "exclusion":
        result = await service.exclude_source(principal, thread.id, thread.revision)
        async with app.uow_factory() as uow:
            row = await uow.email.get(principal, "excluded_source", str(result["source_id"]))
        assert row is not None
        return UUID(str(row.payload["audit_session_id"]))
    if kind == "discussion":
        await service.discussion(principal, thread.id)
    else:
        assert kind == "generated_draft"
        await service.save_generated_draft(
            principal,
            thread.id,
            thread.revision,
            "A revised reply.",
            run_id=uuid4(),
            instruction="Make it shorter.",
        )
    async with app.uow_factory() as uow:
        bound = await service.thread_record(uow.email, principal, thread.id)
    assert bound.session_id is not None
    return bound.session_id


OPERATIONAL = ("archive", "refresh", "exclusion")
THREAD_BOUND = ("discussion", "draft", "generated_draft")


async def assert_reads_continue_during_admission(
    app: Composition, held: HeldDiscovery, kind: str
) -> None:
    """Open the next conversation while a slow MCP server delays one email command."""
    thread, _ = await _seed_draft(app)
    thread = await _unbind_thread_session(app, thread)
    following = await _next_thread(app, thread)
    # Seeding remembered every catalog (ADR-0131); forget it so admission's
    # discovery is live and slow, which is what the owner lock must not wait on.
    app.mcp._discoveries.clear()
    held.hold()
    work = asyncio.create_task(_admit(app, kind, thread))
    try:
        discovering = asyncio.create_task(held.started.wait())
        await asyncio.wait({work, discovering}, timeout=2, return_when="FIRST_COMPLETED")
        discovering.cancel()
        result = await asyncio.wait_for(
            app.services.email.thread(app.principal, following.id), timeout=1
        )
        assert result["id"] == str(following.id)
    finally:
        held.release.set()
        session_id = await work
    assert await _session_pins(app, session_id) == []
    # Operational sessions never render a catalog, so admission starts no
    # server; a thread-bound session still pins its full catalog.
    assert held.started.is_set() is (kind in THREAD_BOUND)


async def claim_session_then_fail(
    app: Composition, held: HeldDiscovery, monkeypatch: pytest.MonkeyPatch
) -> tuple[UUID, EmailThread]:
    """Fail a discussion after it claimed its prepared session; return that session."""
    thread, _ = await _seed_draft(app)
    thread = await _unbind_thread_session(app, thread)
    earlier = await _thread_sessions(app, thread)
    service = app.services.email
    original = service._session_in
    claimed: list[UUID] = []

    async def fail_after_claim(*args: Any, **kwargs: Any) -> Session:
        session = await original(*args, **kwargs)
        claimed.append(session.id)
        raise ConflictError("injected failure after the session was claimed")

    monkeypatch.setattr(service, "_session_in", fail_after_claim)
    held.closed.clear()
    with pytest.raises(ConflictError):
        await service.discussion(app.principal, thread.id)
    [session_id] = claimed
    assert session_id not in earlier
    # Released exactly once, by the rollback hook rather than the unclaimed path.
    assert held.closed == [session_id]
    return session_id, thread


@pytest.mark.parametrize("kind", [*OPERATIONAL, *THREAD_BOUND])
async def test_session_discovery_never_holds_the_owner_email_lock(kind: str) -> None:
    """A slow MCP server must not stall thread reads behind email admission (ADR-0103)."""
    async with held_discovery_email() as (app, held):
        await assert_reads_continue_during_admission(app, held, kind)


@pytest.mark.parametrize("kind", THREAD_BOUND)
async def test_unused_prepared_thread_session_is_released(kind: str) -> None:
    """A concurrently bound session wins, and the prepared catalog leaves no residue."""
    async with held_discovery_email() as (app, held):
        thread, _ = await _seed_draft(app)
        thread = await _unbind_thread_session(app, thread)
        earlier = await _thread_sessions(app, thread)
        # Forget remembered catalogs (ADR-0131) so the losing admission discovers live.
        app.mcp._discoveries.clear()
        held.hold(calls=1)
        work = asyncio.create_task(_admit(app, kind, thread))
        try:
            await asyncio.wait_for(held.started.wait(), timeout=2)
            winner = await asyncio.wait_for(_admit(app, "discussion", thread), timeout=2)
        finally:
            held.release.set()
            loser = await work
        assert loser == winner
        assert await _thread_sessions(app, thread) - earlier == {winner}
        # The loser's catalog and transports are released; the winner's are not.
        assert len(held.closed) == 1 and winner not in held.closed
        assert held.closed[0] not in earlier


async def test_rolled_back_admission_releases_its_claimed_thread_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A claimed catalog is released exactly once when its transaction fails."""
    async with held_discovery_email() as (app, held):
        await claim_session_then_fail(app, held, monkeypatch)
