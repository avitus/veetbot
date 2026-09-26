"""Budgeted People import slices on the normal leased run queue."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import NamedTuple, Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailImportBudget, EmailRecord
from agent_core.domain.errors import (
    BudgetExceededError,
    ConflictError,
    NotFoundError,
    ToolValidationError,
)
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import Sensitivity
from agent_core.domain.messages import (
    AssistantMessage,
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelRequest,
    ResolvedModel,
    TextPart,
)
from agent_core.domain.people import PeopleImportJob, PeopleQuery, PersonIdentifier
from agent_core.domain.people_sources import identifier_occurs, source_id
from agent_core.domain.runs import BudgetScope, OutcomeKind, RunOutcome, RunStatus, Step
from agent_core.model.cost import highest_input_rate
from agent_core.ports.email import EmailStore
from agent_core.ports.models import ModelProvider
from agent_core.ports.people_runtime import PeopleImportControl, PeopleImportFormation
from agent_core.ports.persistence import RepositoryUnitOfWork
from agent_core.runtime.loop import RunContext


class ImportDeferredError(ConflictError):
    """Interactive work takes precedence over another historical-import step."""


class ImportStoppedError(ConflictError):
    """The job no longer authorizes another provider call or derived write."""


class InvalidImportSourceError(ImportStoppedError):
    """Retained source metadata cannot be ordered or safely analyzed."""


def email_evidence_time(record: EmailRecord) -> datetime:
    """Require an aware source timestamp without exposing source payloads."""
    try:
        raw = record.payload["evidence_at"]
        if not isinstance(raw, str):
            raise ValueError("timestamp must be text")
        at = datetime.fromisoformat(raw)
        if at.tzinfo is None or at.utcoffset() is None:
            raise ValueError("timestamp must include its time zone")
        return at
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidImportSourceError("invalid email evidence timestamp") from exc


def reservation_cost(request: ModelRequest, model: ResolvedModel) -> Decimal:
    """Reserve the advertised context/output maximum, including cache and reasoning."""
    price = model.pricing
    input_rate = highest_input_rate(price, request.cache_hints)
    output_rate = price.output_per_mtok
    if price.reasoning_priced_separately:
        if price.reasoning_per_mtok is None:
            raise ImportStoppedError("reasoning price is unavailable")
        output_rate += price.reasoning_per_mtok
    if model.provider != "fake" and (not input_rate or not output_rate):
        raise ImportStoppedError("import model pricing is unavailable")
    return (
        model.limits.context_window_tokens * input_rate
        + (request.maximum_output_tokens or model.limits.max_output_tokens) * output_rate
    ) / Decimal(1_000_000)


class ImportRecordResult(NamedTuple):
    complete: bool
    processed: bool
    rejected: bool = False


class EmailImportProcessor(Protocol):
    async def process(self, record: EmailRecord, job: PeopleImportJob) -> ImportRecordResult: ...


class EmailSourceReader(Protocol):
    async def discover(self) -> bool: ...


class ImportProvider:
    def __init__(
        self,
        worker: ImportSlice,
        provider: ModelProvider,
        *,
        email_budget: Callable[[EmailStore, Principal, Decimal], Awaitable[None]] | None = None,
    ) -> None:
        self.worker = worker
        self.provider = provider
        self.name = provider.name
        self.email_budget = email_budget

    async def close(self) -> None:
        # The composition owns the shared provider's lifetime.
        return None

    async def stream(
        self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        context = self.worker.context
        context.token.raise_if_cancelled()
        context.budgets.check(context.run, BudgetScope.ATTEMPT)
        cost = reservation_cost(request, resolved)
        insufficient = False
        async with (
            context.uow_factory() as uow,
            uow.email.lock(context.principal),
            uow.people.lock(context.principal),
        ):
            job = await self.worker.guard(uow)
            held = sum(job.reservations.values(), Decimal(0))
            if job.spent_usd + held + cost > job.scope.max_cost_usd:
                await self.worker.save(
                    uow, job, state="budget_paused", error_code="budget_exceeded"
                )
                insufficient = True
            else:
                if self.email_budget is not None:
                    try:
                        await self.email_budget(uow.email, context.principal, cost)
                    except BudgetExceededError:
                        await self.worker.save(
                            uow, job, state="budget_paused", error_code="email_budget_exceeded"
                        )
                        insufficient = True
                    else:
                        await uow.email.put(
                            EmailRecord(
                                tenant_id=context.principal.tenant_id,
                                principal_id=context.principal.principal_id,
                                kind="people_import_budget",
                                key=str(attempt.attempt_id),
                                revision=1,
                                created_at=context.clock.now(),
                                updated_at=context.clock.now(),
                                payload=EmailImportBudget(reservation=cost).model_dump(mode="json"),
                            ),
                            expected_revision=0,
                        )
                if not insufficient:
                    await self.worker.save(
                        uow, job, reservations={**job.reservations, attempt.attempt_id: cost}
                    )
        if insufficient:
            raise ImportStoppedError("import budget cannot cover another model call")
        attempt = attempt.model_copy(
            update={
                "run_id": context.run.id,
                "step_number": context.run.model_call_count + 1,
            }
        )
        request = request.model_copy(
            update={
                "metadata": {
                    **request.metadata,
                    "prefix_sha256": hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
                }
            }
        )
        # An interrupted call keeps its reservation. A retry cannot spend it again.
        settled = False
        try:
            async for event in self.provider.stream(request, resolved, attempt):
                if isinstance(event, ModelCompletedEvent):
                    usage = event.turn.usage
                    async with (
                        context.uow_factory() as uow,
                        uow.email.lock(context.principal),
                        uow.people.lock(context.principal),
                    ):
                        job = await self.worker.read(uow)
                        reservations = dict(job.reservations)
                        reservations.pop(attempt.attempt_id, None)
                        spent = job.spent_usd + max(Decimal(0), usage.cost)
                        await self.worker.save(
                            uow,
                            job,
                            reservations=reservations,
                            spent_usd=spent,
                            **(
                                {"state": "failed", "error_code": "provider_cost_overrun"}
                                if spent + sum(reservations.values(), Decimal(0))
                                > job.scope.max_cost_usd
                                else {}
                            ),
                        )
                        if self.email_budget is not None:
                            record = await uow.email.get(
                                context.principal, "people_import_budget", str(attempt.attempt_id)
                            )
                            assert record is not None
                            await uow.email.put(
                                record.model_copy(
                                    update={
                                        "revision": record.revision + 1,
                                        "updated_at": context.clock.now(),
                                        "payload": {
                                            **record.payload,
                                            "settled_cost": str(max(Decimal(0), usage.cost)),
                                        },
                                    }
                                ),
                                expected_revision=record.revision,
                            )
                    settled = True
                    await context.budgets.record_model_usage(
                        context.run,
                        usage,
                        step=Step(
                            run_id=context.run.id,
                            step_number=attempt.step_number,
                            started_at=attempt.started_at,
                        ),
                        attempt=attempt,
                        request=request,
                        resolved_model=resolved,
                        stop_reason=event.stop_reason,
                    )
                yield event
        finally:
            if not settled:
                async with context.uow_factory() as uow, uow.people.lock(context.principal):
                    job = await self.worker.read(uow)
                    if job.state == "running":
                        await self.worker.save(
                            uow,
                            job,
                            state="failed",
                            failures=job.failures + 1,
                            error_code="provider_usage_uncertain",
                        )


class ImportSlice:
    def __init__(self, service: PeopleImportControl, context: RunContext, job_id: UUID) -> None:
        self.service = service
        self.context = context
        self.job_id = job_id

    async def read(self, uow: RepositoryUnitOfWork) -> PeopleImportJob:
        return await self.service._get(
            uow, self.context.principal, self.job_id, Sensitivity.RESTRICTED
        )

    async def save(
        self, uow: RepositoryUnitOfWork, job: PeopleImportJob, **updates: object
    ) -> PeopleImportJob:
        updated = PeopleImportJob.model_validate(
            {
                **job.model_dump(),
                **updates,
                "revision": job.revision + 1,
                "updated_at": max(
                    self.context.clock.now(), job.updated_at + timedelta(microseconds=1)
                ),
            }
        )
        stored = await uow.people.put(updated, expected_revision=job.revision)
        assert isinstance(stored, PeopleImportJob)
        return stored

    async def guard(self, uow: RepositoryUnitOfWork) -> PeopleImportJob:
        context = self.context
        context.token.raise_if_cancelled()
        job = await self.read(uow)
        run = await uow.runs.get(context.run.id, context.principal)
        if (
            job.run_id != run.id
            or job.state != "running"
            or run.cancel_requested_at is not None
            or not self.service.capture_available
            or (job.scope.account_ids and not self.service.email_capture_available)
            or job.implementation_sha256 != self.service.implementation_identity()
            or (
                job.scope.email_source == "mailbox"
                and job.account_servers
                != {
                    account: self.service.account_servers.get(account, {})
                    for account in job.scope.account_ids
                }
            )
        ):
            raise ImportStoppedError("import authorization changed")
        if await uow.runs.has_higher_priority_work(context.principal, context.run.priority):
            raise ImportDeferredError("People import is waiting for interactive work")
        await self.service.validate_sources(
            uow, context.principal, job.scope, Sensitivity.RESTRICTED
        )
        try:
            current_identities = await self.service.identity_revisions(
                uow, context.principal, job.scope.person_ids, Sensitivity.RESTRICTED
            )
        except (NotFoundError, ToolValidationError) as exc:
            raise ImportStoppedError("import identity is unavailable") from exc
        if current_identities != job.identity_revisions:
            raise ImportStoppedError("import identity selection changed")
        await uow.events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type="people.import.updated",
                actor_type="runtime",
                payload={},
            ),
            lease=context.lease,
        )
        return job

    async def selected(
        self, uow: RepositoryUnitOfWork, job: PeopleImportJob, event: EventEnvelope
    ) -> bool:
        owner = self.context.principal
        source_text = self.service.source_text(event, owner)
        if source_text is None:
            return False
        source_key = source_id(owner, event.session_id, event.sequence)
        if source_key in job.scope.excluded_source_ids or await uow.people.source_suppressed(
            owner, source_key
        ):
            return False
        if not job.scope.person_ids:
            return True
        for person_id in job.scope.person_ids:
            aliases = await uow.people.query(
                PeopleQuery(
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    kinds=["identifier"],
                    person_id=person_id,
                    distinct_assignments=True,
                    sensitivity_ceiling=Sensitivity.RESTRICTED,
                    limit=100,
                )
            )
            if len(aliases) > 100:
                raise ImportStoppedError("person filter exceeds the bounded identifier window")
            for alias in aliases:
                if (
                    isinstance(alias, PersonIdentifier)
                    and alias.verification == "owner_confirmed"
                    and alias.valid_from <= event.created_at
                    and (alias.valid_to is None or event.created_at < alias.valid_to)
                    and identifier_occurs(source_text, alias.value, alias.identifier_kind)
                ):
                    return True
        return False

    async def execute(
        self,
        factory: Callable[[ImportSlice], PeopleImportFormation],
        email_factory: Callable[[ImportSlice, PeopleImportJob], EmailImportProcessor] | None = None,
        mailbox_factory: Callable[[ImportSlice, PeopleImportJob], EmailSourceReader] | None = None,
    ) -> RunOutcome:
        context = self.context
        async with context.uow_factory() as uow, uow.people.lock(context.principal):
            job = await self.read(uow)
            if job.state not in {"queued", "running"} or job.run_id != context.run.id:
                return finished("People import is no longer active.")
            job = await self.save(uow, job, state="running", error_code=None)
        try:
            async with context.uow_factory() as uow, uow.people.lock(context.principal):
                job = await self.guard(uow)
            if job.scope.email_source == "mailbox" and not job.mailbox.complete:
                if mailbox_factory is None:
                    raise ImportStoppedError("mailbox discovery is unavailable")
                if not await mailbox_factory(self, job).discover():
                    async with context.uow_factory() as uow, uow.people.lock(context.principal):
                        job = await self.guard(uow)
                        await self.save(uow, job, state="queued")
                    return finished(
                        "Mailbox read progress has been saved; analysis follows discovery."
                    )
            async with context.uow_factory() as uow:
                job = await self.guard(uow)
                retry_source = job.retry_source
                remaining = 1 if retry_source else job.scope.max_records - job.records_read
                events = (
                    await uow.events.list_window(
                        context.principal,
                        session_ids=job.scope.session_ids,
                        since=job.scope.since,
                        until=job.scope.until,
                        after=(job.event_after_at, job.event_after_id)
                        if job.event_after_at is not None and job.event_after_id is not None
                        else None,
                        limit=min(256, remaining),
                    )
                    if remaining and job.scope.session_ids and retry_source is None
                    else []
                )
                try:
                    mail = (
                        await uow.email.list_semantic_window(
                            context.principal,
                            account_ids=job.scope.account_ids,
                            since=job.scope.since,
                            until=job.scope.until,
                            after=(job.email_after_at, job.email_after_key)
                            if job.email_after_at is not None and job.email_after_key is not None
                            else None,
                            limit=min(100, remaining),
                        )
                        if remaining and job.scope.account_ids and retry_source is None
                        else []
                    )
                except ValueError as exc:
                    raise InvalidImportSourceError("invalid retained email source") from exc
                if retry_source is not None:
                    if retry_source.session_id not in job.scope.session_ids:
                        raise ImportStoppedError("retry source is outside the approved scope")
                    events = await uow.events.list_after(
                        retry_source.session_id,
                        retry_source.sequence - 1,
                        context.principal,
                        limit=1,
                    )
                    if (
                        len(events) != 1
                        or events[0].id != retry_source.event_id
                        or events[0].sequence != retry_source.sequence
                        or events[0].created_at != retry_source.created_at
                        or not job.scope.since <= events[0].created_at < job.scope.until
                    ):
                        raise ImportStoppedError("the failed source is no longer available")
                selected_ids = {
                    event.id for event in events if await self.selected(uow, job, event)
                }
            service = factory(self) if events else None
            email = email_factory(self, job) if email_factory is not None and mail else None
            if mail and email is None:
                raise ImportStoppedError("email import worker is unavailable")
            ordered: list[tuple[datetime, str, EventEnvelope | EmailRecord]] = [
                (event.created_at, f"chat:{event.id:020d}", event) for event in events
            ] + [
                (
                    email_evidence_time(record),
                    f"email:{record.key}",
                    record,
                )
                for record in mail
            ]
            ordered.sort(key=lambda item: (item[0], item[1]))
            consumed = 0
            for at, _key, record in ordered[:remaining]:
                selected = isinstance(record, EmailRecord) or record.id in selected_ids
                incomplete = False
                if selected and context.run.model_call_count >= 3:
                    break
                if isinstance(record, EmailRecord):
                    assert email is not None
                    async with context.uow_factory() as uow:
                        job = await self.guard(uow)
                    result = await email.process(record, job)
                    incomplete = result.rejected
                    if not (result.complete or incomplete):
                        break
                    selected = result.processed
                    cursor: dict[str, object] = {
                        "email_after_at": at,
                        "email_after_key": record.key,
                        "email_current_key": None,
                        "email_passage_offset": None,
                        "email_current_processed": False,
                        "email_current_failed": False,
                    }
                else:
                    if selected:
                        assert service is not None
                        formed = await service.run(
                            trigger="people_import",
                            scope="general",
                            session_id=record.session_id,
                            source_window=(record,),
                        )
                        incomplete = bool(formed.run.fallback_stages)
                    cursor = {"event_after_at": record.created_at, "event_after_id": record.id}
                async with context.uow_factory() as uow, uow.people.lock(context.principal):
                    job = await self.guard(uow)
                    retrying = job.retry_source is not None
                    if incomplete:
                        pause: dict[str, object]
                        if isinstance(record, EmailRecord):
                            # The record stays unread and its passage cursor stays
                            # before the rejected passage, so resume reassesses it.
                            current = job.email_current_key == record.key
                            pause = {
                                "failures": job.failures
                                + int(not (current and job.email_current_failed)),
                                "email_current_key": record.key,
                                "email_passage_offset": job.email_passage_offset
                                if current
                                else None,
                                "email_current_processed": current and job.email_current_processed,
                                "email_current_failed": True,
                            }
                        else:
                            from agent_core.domain.people_imports import PeopleImportRetry

                            pause = {
                                "records_read": job.records_read + int(not retrying),
                                "failures": job.failures + int(not retrying),
                                "retry_source": PeopleImportRetry(
                                    session_id=record.session_id,
                                    event_id=record.id,
                                    sequence=record.sequence,
                                    created_at=record.created_at,
                                ),
                            }
                        await self.save(
                            uow, job, state="failed", error_code="analysis_incomplete", **pause
                        )
                        return finished(
                            "People import paused at a source that needs another analysis attempt."
                        )
                    await self.save(
                        uow,
                        job,
                        **cursor,
                        records_read=job.records_read + int(not retrying),
                        records_processed=job.records_processed + int(selected),
                        records_excluded=job.records_excluded + int(not selected),
                        failures=job.failures - int(retrying),
                        retry_source=None,
                    )
                consumed += 1
            async with context.uow_factory() as uow, uow.people.lock(context.principal):
                job = await self.guard(uow)
                complete = (
                    retry_source is None
                    and consumed == len(ordered)
                    and (not job.scope.session_ids or len(events) < min(256, remaining))
                    and (not job.scope.account_ids or len(mail) < min(100, remaining))
                )
                capped = job.records_read >= job.scope.max_records
                await self.save(
                    uow,
                    job,
                    state="completed" if complete or capped else "queued",
                    source_read_complete=complete and not job.mailbox.partial,
                    analysis_complete=complete and job.failures == 0 and not job.mailbox.partial,
                    error_code=(
                        "record_limit"
                        if (capped and not complete) or job.mailbox.partial
                        else "analysis_incomplete"
                        if job.failures
                        else None
                    ),
                )
        except ImportDeferredError:
            async with context.uow_factory() as uow, uow.people.lock(context.principal):
                job = await self.read(uow)
                if job.state == "running":
                    await self.save(uow, job, state="queued", error_code="waiting_for_chat")
        except ImportStoppedError as exc:
            async with context.uow_factory() as uow, uow.people.lock(context.principal):
                job = await self.read(uow)
                if job.state == "running":
                    await self.save(
                        uow,
                        job,
                        state="failed",
                        failures=job.failures + 1,
                        error_code="invalid_source"
                        if isinstance(exc, InvalidImportSourceError)
                        else "authority_changed",
                    )
        return finished("People import progress has been saved.")


def finished(text: str) -> RunOutcome:
    return RunOutcome(
        kind=OutcomeKind.COMPLETED, final_message=AssistantMessage(content=[TextPart(text=text)])
    )


class PeopleImportRunner:
    def __init__(
        self,
        service: PeopleImportControl,
        factory: Callable[[ImportSlice], PeopleImportFormation],
        *,
        dispatch_continuations: bool = True,
    ) -> None:
        self.dispatch_continuations = dispatch_continuations
        self.service = service
        self.factory = factory
        self.email_factory: (
            Callable[[ImportSlice, PeopleImportJob], EmailImportProcessor] | None
        ) = None
        self.mailbox_factory: Callable[[ImportSlice, PeopleImportJob], EmailSourceReader] | None = (
            None
        )

    async def __call__(self, context: RunContext) -> RunOutcome | None:
        async with context.uow_factory() as uow:
            session = await uow.sessions.get(context.run.session_id, context.principal)
        key = session.metadata.get("people_import_job_id")
        if not isinstance(key, str):
            return None
        return await ImportSlice(self.service, context, UUID(key)).execute(
            self.factory, self.email_factory, self.mailbox_factory
        )

    async def after_run(self, run_id: UUID, owner: Principal) -> None:
        next_run = None
        async with self.service.factory() as uow, uow.people.lock(owner):
            run = await uow.runs.get(run_id, owner)
            session = await uow.sessions.get(run.session_id, owner)
            key = session.metadata.get("people_import_job_id")
            if not isinstance(key, str):
                return
            job = await self.service._get(uow, owner, UUID(key), Sensitivity.RESTRICTED)
            if (
                job.run_id == run_id
                and job.state in {"queued", "running"}
                and run.status in {RunStatus.FAILED, RunStatus.CANCELLED}
            ):
                await uow.people.put(
                    job.model_copy(
                        update={
                            "state": "cancelled" if run.status == RunStatus.CANCELLED else "failed",
                            "error_code": "run_interrupted",
                            "revision": job.revision + 1,
                            "updated_at": max(
                                self.service.clock.now(), job.updated_at + timedelta(microseconds=1)
                            ),
                        }
                    ),
                    expected_revision=job.revision,
                )
            if job.run_id == run_id and job.state == "queued" and run.status == RunStatus.COMPLETED:
                updated = await self.service.enqueue(uow, owner, job)
                next_run = updated.run_id
        if (
            self.dispatch_continuations
            and next_run is not None
            and self.service.dispatch is not None
        ):
            await self.service.dispatch(next_run)
