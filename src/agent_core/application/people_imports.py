"""Revision-bound import previews, durable dispatch, cancellation and accounting."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailAccount
from agent_core.domain.errors import ConflictError, NotFoundError, ToolValidationError
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import SENSITIVITY_ORDER, Sensitivity
from agent_core.domain.people import PeopleImportJob, PeopleQuery, Person, PersonIdentifier
from agent_core.domain.people_imports import (
    PeopleImportCancel,
    PeopleImportRequest,
    PeopleImportScope,
    PeopleImportView,
)
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, Run, RunLimits, RunStatus
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.domain.views import Page
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory


class PeopleImportService:
    def __init__(self, factory: UnitOfWorkFactory, clock: Clock) -> None:
        self.factory = factory
        self.clock = clock
        self.implementation_identity: Callable[[], str] = self._unconfigured_identity
        self.source_text: Callable[[EventEnvelope, Principal], str | None] = (
            self._unconfigured_source
        )
        self.capture_available = False
        self.queue_priority = 10
        self.email_capture_available = False
        self.account_servers: dict[str, dict[str, str]] = {}
        self.dispatch: Callable[[UUID], Awaitable[None]] | None = None

    @staticmethod
    def _unconfigured_source(_event: EventEnvelope, _owner: Principal) -> str | None:
        return None

    @staticmethod
    def _unconfigured_identity() -> str:
        raise ConflictError("People import implementation identity is not configured")

    @staticmethod
    def view(job: PeopleImportJob) -> PeopleImportView:
        return PeopleImportView(
            id=job.id,
            audit_session_id=job.audit_session_id,
            revision=job.revision,
            state=job.state,
            scope=job.scope,
            records_read=job.records_read,
            mailbox_records_read=job.mailbox.records_read,
            mailbox_read_complete=job.mailbox.complete and not job.mailbox.partial,
            records_processed=job.records_processed,
            records_excluded=job.records_excluded,
            failures=job.failures,
            spent_usd=job.spent_usd,
            reserved_usd=sum(job.reservations.values(), Decimal("0")),
            source_read_complete=job.source_read_complete,
            analysis_complete=job.analysis_complete,
            known_records=job.known_records,
            remaining_records=None
            if job.known_records is None
            else max(0, job.known_records - job.records_read),
            coverage=job.coverage,
            error_code=job.error_code,
            run_id=job.run_id,
        )

    async def _get(
        self, uow: RepositoryUnitOfWork, owner: Principal, key: UUID, ceiling: Sensitivity
    ) -> PeopleImportJob:
        row = await uow.people.get(owner, key, ceiling=ceiling)
        if not isinstance(row, PeopleImportJob):
            raise NotFoundError("People import not found")
        return row

    async def get(self, owner: Principal, key: UUID, *, ceiling: Sensitivity) -> PeopleImportView:
        require_scope(owner, "people.read")
        async with self.factory() as uow:
            return self.view(await self._get(uow, owner, key, ceiling))

    async def list(
        self,
        owner: Principal,
        *,
        ceiling: Sensitivity,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[PeopleImportView]:
        require_scope(owner, "people.read")
        query = PeopleQuery(
            tenant_id=owner.tenant_id,
            principal_id=owner.principal_id,
            kinds=["import_job"],
            sensitivity_ceiling=ceiling,
            limit=limit,
        )
        binding = hashlib.sha256(query.model_dump_json().encode()).hexdigest()
        watermark = None
        if cursor is not None:
            try:
                if len(cursor) > 2048:
                    raise ValueError("cursor too long")
                decoded = json.loads(base64.urlsafe_b64decode(cursor))
                if decoded["binding"] != binding:
                    raise ValueError("changed cursor binding")
                query = query.model_copy(update={"after": UUID(decoded["after"])})
                watermark = int(decoded["watermark"])
            except (ValueError, KeyError, TypeError) as exc:
                raise ConflictError("import cursor changed; refresh the list") from exc
        async with self.factory() as uow, uow.people.lock(owner):
            before = await uow.people.watermark(owner)
            if watermark is not None and watermark != before:
                raise ConflictError("imports changed; refresh the list")
            rows = await uow.people.query(query)
            next_cursor = None
            if len(rows) > limit:
                next_cursor = base64.urlsafe_b64encode(
                    json.dumps(
                        {
                            "binding": binding,
                            "watermark": before,
                            "after": str(rows[limit - 1].id),
                        }
                    ).encode()
                ).decode()
            return Page(
                items=[self.view(row) for row in rows[:limit] if isinstance(row, PeopleImportJob)],
                next_cursor=next_cursor,
            )

    async def validate_sources(
        self,
        uow: RepositoryUnitOfWork,
        owner: Principal,
        scope: PeopleImportScope,
        ceiling: Sensitivity,
    ) -> None:
        if scope.session_ids:
            require_scope(owner, "session.read")
            for session_id in scope.session_ids:
                await uow.sessions.get(session_id, owner)
        if scope.account_ids:
            require_scope(owner, "email.read")
            for account_id in scope.account_ids:
                if scope.email_source == "mailbox":
                    server = self.account_servers.get(account_id, {}).get("read")
                    if server is None:
                        raise NotFoundError("import mailbox is unavailable")
                    require_scope(owner, f"mcp.{server}.use")
                row = await uow.email.get(owner, "account", account_id)
                if row is None or EmailAccount.model_validate(row.payload).status != "ready":
                    raise NotFoundError("import account is unavailable")
        for person_id in scope.person_ids:
            person = await uow.people.get(owner, person_id, ceiling=ceiling)
            if not isinstance(person, Person) or person.state == "merged":
                raise NotFoundError("import person is unavailable")
            aliases = await uow.people.query(
                PeopleQuery(
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    kinds=["identifier"],
                    person_id=person_id,
                    sensitivity_ceiling=ceiling,
                    limit=100,
                )
            )
            if not any(
                isinstance(alias, PersonIdentifier)
                and alias.verification == "owner_confirmed"
                and (
                    scope.email_source != "mailbox"
                    or (
                        alias.identifier_kind == "email"
                        and alias.valid_from < scope.until
                        and (alias.valid_to is None or alias.valid_to > scope.since)
                    )
                )
                for alias in aliases
            ):
                raise ToolValidationError(
                    "person-filtered import requires a confirmed identifier; mailbox discovery "
                    "requires an email address valid in the selected window"
                )

    async def submit(
        self, owner: Principal, request: PeopleImportRequest, *, key: str, ceiling: Sensitivity
    ) -> PeopleImportView:
        require_scope(owner, "people.write")
        if not key or len(key) > 200:
            raise ToolValidationError("invalid import idempotency key")
        if SENSITIVITY_ORDER[ceiling] < SENSITIVITY_ORDER[Sensitivity.SENSITIVE]:
            raise ToolValidationError("People imports require the sensitive ceiling")
        request = PeopleImportRequest.model_validate(request.model_dump())
        digest = hashlib.sha256(request.model_dump_json().encode()).hexdigest()
        replay_key = (
            "people-import:"
            + hashlib.sha256(
                json.dumps([owner.tenant_id, owner.principal_id, key]).encode()
            ).hexdigest()
        )
        run_id = None
        async with self.factory() as uow, uow.people.lock(owner):
            audit_session = await uow.sessions.get(request.session_id, owner)
            prior = await uow.events.get_by_derivation(replay_key, owner)
            if prior is not None:
                if prior.payload.get("request_hash") != digest:
                    raise ConflictError("import idempotency key was reused")
                return self.view(
                    await self._get(uow, owner, UUID(prior.payload["job_id"]), ceiling)
                )
            await self.validate_sources(uow, owner, request.scope, ceiling)
            now = self.clock.now()
            if request.phase == "preview":
                known: int | None = 0 if not request.scope.account_ids else None
                for session_id in request.scope.session_ids:
                    events = await uow.events.list_after(session_id, 0, owner, limit=257)
                    if len(events) == 257:
                        known = None
                    elif known is not None:
                        known += sum(
                            request.scope.since <= event.created_at < request.scope.until
                            for event in events
                        )
                identities: dict[UUID, int] = {}
                for person_id in request.scope.person_ids:
                    person = await uow.people.get(owner, person_id, ceiling=ceiling)
                    if not isinstance(person, Person):
                        raise NotFoundError("import person is unavailable")
                    identities[person.id] = person.revision
                    aliases = await uow.people.query(
                        PeopleQuery(
                            tenant_id=owner.tenant_id,
                            principal_id=owner.principal_id,
                            kinds=["identifier"],
                            person_id=person_id,
                            sensitivity_ceiling=ceiling,
                            limit=100,
                        )
                    )
                    if len(aliases) > 100:
                        raise ToolValidationError("import person has too many identifiers")
                    identities.update({alias.id: alias.revision for alias in aliases})
                job = PeopleImportJob(
                    identity_revisions=identities,
                    id=uuid5(NAMESPACE_URL, replay_key),
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    created_at=now,
                    updated_at=now,
                    sensitivity=Sensitivity.SENSITIVE,
                    scope=request.scope,
                    audit_session_id=audit_session.id,
                    known_records=known,
                    expires_at=now + timedelta(minutes=10),
                    request_hash=digest,
                    implementation_sha256=self.implementation_identity(),
                    account_servers={
                        account: dict(self.account_servers[account])
                        for account in request.scope.account_ids
                    }
                    if request.scope.email_source == "mailbox"
                    else {},
                    coverage=(
                        "Explicit mailbox discovery and retained sources in the selected range; "
                        "provider pages and the record cap may limit coverage."
                        if request.scope.email_source == "mailbox"
                        else "Selected retained sources only; "
                        "unavailable earlier history is not covered."
                    ),
                )
                await uow.people.put(job, expected_revision=0)
            else:
                assert request.operation_id is not None
                job = await self._get(uow, owner, request.operation_id, ceiling)
                if request.phase == "resume":
                    if (
                        job.state not in {"budget_paused", "failed", "cancelled"}
                        or job.revision != request.expected_revision
                        or job.worker_session_id is None
                        or job.scope.model_dump(exclude={"max_records", "max_cost_usd"})
                        != request.scope.model_dump(exclude={"max_records", "max_cost_usd"})
                        or request.scope.max_records < job.scope.max_records
                        or request.scope.max_cost_usd < job.scope.max_cost_usd
                        or job.reservations
                    ):
                        raise ConflictError("import resume changed scope or has unresolved usage")
                    job = job.model_copy(update={"scope": request.scope, "error_code": None})
                elif (
                    job.state != "preview"
                    or job.revision != request.expected_revision
                    or job.expires_at <= now
                    or job.scope != request.scope
                ):
                    raise ConflictError("import preview changed or expired")
                if (
                    not self.capture_available
                    or (request.scope.account_ids and not self.email_capture_available)
                    or job.implementation_sha256 != self.implementation_identity()
                    or (
                        request.scope.email_source == "mailbox"
                        and job.account_servers
                        != {
                            account: self.account_servers.get(account, {})
                            for account in request.scope.account_ids
                        }
                    )
                ):
                    raise ConflictError(
                        "the selected source policy is unavailable "
                        "or the import configuration changed"
                    )
                if self.dispatch is None:
                    raise ConflictError("People import worker is unavailable")
                jobs = PeopleQuery(
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    kinds=["import_job"],
                    sensitivity_ceiling=Sensitivity.RESTRICTED,
                    limit=100,
                )
                while True:
                    page = await uow.people.query(jobs)
                    for row in page[:100]:
                        # A completed/cancelled job can still have a settling worker run.
                        # The run's terminal state controls admission of another slice.
                        if isinstance(row, PeopleImportJob) and row.run_id is not None:
                            run = await uow.runs.get(row.run_id, owner)
                            if run.status not in TERMINAL_RUN_STATUSES:
                                raise ConflictError("another People import is still active")
                    if len(page) <= 100:
                        break
                    jobs = jobs.model_copy(update={"after": page[99].id})
                if job.worker_session_id is None:
                    worker = Session(
                        id=uuid5(job.id, "worker-session"),
                        tenant_id=owner.tenant_id,
                        principal_id=owner.principal_id,
                        agent_id=audit_session.agent_id,
                        agent_version=audit_session.agent_version,
                        status=SessionStatus.ACTIVE,
                        title="People history import",
                        created_at=now,
                        updated_at=now,
                        metadata={
                            "purpose": "people-import",
                            "people_import_job_id": str(job.id),
                            "email_account_servers": job.account_servers,
                        },
                    )
                    await uow.sessions.create(worker)
                    job = job.model_copy(update={"worker_session_id": worker.id})
                job = await self.enqueue(uow, owner, job)
                run_id = job.run_id
            await uow.events.append(
                NewEvent(
                    session_id=request.session_id,
                    run_id=None,
                    event_type="people.import_receipt",
                    actor_type="principal",
                    actor_id=owner.principal_id,
                    derivation_key=replay_key,
                    payload={"request_hash": digest, "job_id": str(job.id)},
                )
            )
        if run_id is not None:
            assert self.dispatch is not None
            await self.dispatch(run_id)
            async with self.factory() as uow:
                return self.view(await self._get(uow, owner, job.id, ceiling))
        return self.view(job)

    async def enqueue(
        self, uow: RepositoryUnitOfWork, owner: Principal, job: PeopleImportJob
    ) -> PeopleImportJob:
        assert job.worker_session_id is not None
        worker = await uow.sessions.get(job.worker_session_id, owner)
        now = self.clock.now()
        remaining = job.scope.max_cost_usd - job.spent_usd - sum(job.reservations.values())
        ready_at = now + timedelta(seconds=30) if job.error_code == "waiting_for_chat" else now
        run = Run(
            id=uuid5(job.id, f"slice:{job.revision}"),
            session_id=worker.id,
            tenant_id=owner.tenant_id,
            principal_scopes=set(owner.scopes),
            agent_id=worker.agent_id,
            agent_version=worker.agent_version,
            status=RunStatus.QUEUED,
            limits=RunLimits(
                max_steps=12,
                max_model_calls=12,
                max_tool_calls=100,
                max_cost=remaining,
                deadline_at=ready_at + timedelta(seconds=120),
            ),
            priority=self.queue_priority,
            scheduled_for=ready_at,
            deadline_at=ready_at + timedelta(seconds=120),
            created_at=now,
            updated_at=now,
        )
        if uow.queue is None:
            await uow.runs.create(run)
        else:
            await uow.queue.enqueue(run, priority=run.priority, scheduled_for=ready_at)
        updated = job.model_copy(
            update={
                "state": "queued",
                "run_id": run.id,
                "revision": job.revision + 1,
                "updated_at": max(now, job.updated_at + timedelta(microseconds=1)),
            }
        )
        stored = await uow.people.put(updated, expected_revision=job.revision)
        assert isinstance(stored, PeopleImportJob)
        return stored

    async def cancel(
        self,
        owner: Principal,
        job_id: UUID,
        request: PeopleImportCancel,
        *,
        key: str,
        ceiling: Sensitivity,
    ) -> PeopleImportView:
        require_scope(owner, "people.write")
        if not key or len(key) > 200:
            raise ToolValidationError("invalid import idempotency key")
        digest = hashlib.sha256(
            json.dumps([str(job_id), request.model_dump()]).encode()
        ).hexdigest()
        replay_key = (
            "people-import-cancel:"
            + hashlib.sha256(
                json.dumps([owner.tenant_id, owner.principal_id, key]).encode()
            ).hexdigest()
        )
        async with self.factory() as uow, uow.people.lock(owner):
            job = await self._get(uow, owner, job_id, ceiling)
            prior = await uow.events.get_by_derivation(replay_key, owner)
            if prior is not None:
                if prior.payload.get("request_hash") != digest:
                    raise ConflictError("import cancellation idempotency key was reused")
                return self.view(job)
            if job.revision != request.expected_revision:
                raise ConflictError("import progress changed; refresh before cancellation")
            if job.state == "completed":
                raise ConflictError("import already completed")
            if job.run_id:
                run = await uow.runs.get(job.run_id, owner)
                if run.status not in TERMINAL_RUN_STATUSES:
                    await uow.runs.request_cancellation(run.id, run.status)
            stored = await uow.people.put(
                job.model_copy(
                    update={
                        "state": "cancelled",
                        "revision": job.revision + 1,
                        "updated_at": max(
                            self.clock.now(), job.updated_at + timedelta(microseconds=1)
                        ),
                    }
                ),
                expected_revision=job.revision,
            )
            assert isinstance(stored, PeopleImportJob)
            await uow.events.append(
                NewEvent(
                    session_id=job.audit_session_id,
                    run_id=None,
                    event_type="people.import_receipt",
                    actor_type="principal",
                    actor_id=owner.principal_id,
                    derivation_key=replay_key,
                    payload={"request_hash": digest, "job_id": str(job.id)},
                )
            )
            return self.view(stored)
