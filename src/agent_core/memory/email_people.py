"""Separately evaluated People formation from verified email source passages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from typing import Literal
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.correspondence import (
    RETAINED_TEXT_WINDOW,
    CorrespondenceSummaryWork,
    EmailCorrespondenceSummary,
    RetainedEmailMessage,
    header_text,
    summary_text,
)
from agent_core.domain.email import EMAIL_HISTORY_DAYS, EmailRecord
from agent_core.domain.email_people import EmailPeopleFact
from agent_core.domain.email_people_evidence import EmailPeopleEvidence
from agent_core.domain.email_semantics import (
    EmailSemanticFact,
    EmailSemanticSource,
    semantic_source_key,
)
from agent_core.domain.errors import (
    ConflictError,
    NotFoundError,
    ToolTrustRejectedError,
    ToolValidationError,
)
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import (
    MemoryAuthority,
    MemoryCandidate,
    MemoryDerivation,
    MemoryLongevity,
    MemoryRecord,
    Polarity,
    Portability,
    Sensitivity,
)
from agent_core.domain.people import (
    CORRESPONDENCE_SUMMARY_GENERATOR,
    InteractionSummaryProvenance,
    PeopleInteraction,
    PeopleQuery,
    PeopleSource,
    observed_email_label,
    summary_pending,
)
from agent_core.domain.people_extraction import PeopleClaim
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import Run
from agent_core.memory.communication_sources import FormationSource, FormationSourceKind
from agent_core.memory.email_semantics import EmailSemanticFormationService
from agent_core.memory.formation import GovernedMemoryService
from agent_core.memory.people_correspondence import project_correspondence
from agent_core.memory.people_formation import (
    PreparedPeople,
    email_source_id,
    owner_references,
    persist_people,
    prepare_people,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

EMAIL_PEOPLE_POLICY = "email-semantic@2"


class EmailPeopleFormationService(EmailSemanticFormationService):
    """Reuse the normal assessment; apply source checks and governance without provider calls."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        principal: Principal,
        *,
        provider: str,
        model: str,
        evidence: EmailPeopleEvidence | None = None,
        import_window: tuple[datetime, datetime] | None = None,
        import_guard: Callable[[RepositoryUnitOfWork], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(uow_factory, clock, ids, principal, provider=provider, model=model)
        self._people_evidence = evidence
        if (import_window is None) != (import_guard is None):
            raise ValueError("historical email requires both an explicit window and a job guard")
        self._import_window = import_window
        self._import_guard = import_guard
        self._governed = GovernedMemoryService(
            uow_factory,
            clock,
            ids,
            principal,
            policy_version=EMAIL_PEOPLE_POLICY,
            people_enabled=True,
        )

    async def _admit_import(self, uow: RepositoryUnitOfWork, source: EmailSemanticSource) -> None:
        if self._import_window is None:
            return
        if not self._import_window[0] <= source.sent_at < self._import_window[1]:
            raise ToolValidationError("email is outside the explicitly approved import window")
        assert self._import_guard is not None
        await self._import_guard(uow)

    @property
    def enabled(self) -> bool:
        # Composition selects this source policy explicitly; evaluation artifacts
        # remain quality reports rather than runtime capability switches (ADR-0101).
        return True

    @property
    def people_enabled(self) -> bool:
        return self.enabled

    async def import_passage(
        self, record: EmailRecord, *, after_offset: int | None
    ) -> EmailSemanticSource | None:
        """Recover the next retained passage and verify it against immutable source events."""
        if self._import_window is None or not self.enabled:
            raise ConflictError("historical email capture is unavailable")
        passages = self._retained_passages(record)
        offsets = sorted(int(key) for key in passages if str(key).isdigit())
        offset = next((key for key in offsets if after_offset is None or key > after_offset), None)
        if offset is None:
            return None
        source = self._pending_source(record, offset)
        async with (
            self._uow_factory() as uow,
            uow.email.lock(self._principal),
            uow.people.lock(self._principal),
        ):
            await self._admit_import(uow, source)
            current = await uow.email.get(self._principal, record.kind, record.key)
            if current is None or current.payload.get("excluded"):
                raise ConflictError("email import source was excluded")
            if await self.bulk_source(uow, source.account_id, source.provider_thread_id):
                # ADR-0116: a retained passage from bulk mail, by census or by the
                # persisted verdict, is skipped and counted as excluded, not analyzed.
                return None
            source, _header, _event = await self._verify_passage(uow, source, passages)
            if await uow.people.source_suppressed(
                self._principal, email_source_id(self._principal, source)
            ):
                raise ConflictError("People email source was erased")
            return source

    async def reproject_correspondence(self, record: EmailRecord) -> bool:
        """Project one retained message's headers into People again; no model is called.

        The directory repair (ADR-0121) uses this for mail registered while
        correspondence could not run. It returns False for bulk, excluded or
        suppressed mail, and raises for mail that no longer verifies.
        """
        if not self.enabled:
            return False
        try:
            passages = self._retained_passages(record)
        except ToolTrustRejectedError:
            return False
        offsets = sorted(int(key) for key in passages if str(key).isdigit())
        if not offsets:
            return False
        source = self._pending_source(record, offsets[0])
        async with (
            self._uow_factory() as uow,
            uow.email.lock(self._principal),
            uow.people.lock(self._principal),
        ):
            current = await uow.email.get(self._principal, record.kind, record.key)
            if current is None or current.payload.get("excluded"):
                return False
            if await self.bulk_source(uow, source.account_id, source.provider_thread_id):
                return False
            source, header, event = await self._verify_passage(uow, source, passages)
            if await uow.people.source_suppressed(
                self._principal, email_source_id(self._principal, source)
            ):
                return False
            await project_correspondence(
                uow, self._principal, source, header, event, self._clock.now()
            )
            return True

    async def next_correspondence_summaries(
        self, account_ids: Sequence[str], *, limit: int
    ) -> list[CorrespondenceSummaryWork]:
        """Exchanges awaiting a summary, newest first, with verified passages (ADR-0126).

        An exchange whose every source in these accounts became bulk mail or no
        longer verifies abstains here for good, so later refreshes skip it.
        """
        if not self.enabled or limit < 1:
            return []
        accounts = set(account_ids)
        now = self._clock.now()
        query = PeopleQuery(
            tenant_id=self._principal.tenant_id,
            principal_id=self._principal.principal_id,
            kinds=["interaction"],
            summary_pending=True,
            sort="history",
            since=now - timedelta(days=EMAIL_HISTORY_DAYS),
            sensitivity_ceiling=Sensitivity.RESTRICTED,
            limit=100,
        )
        work: list[CorrespondenceSummaryWork] = []
        async with (
            self._uow_factory() as uow,
            uow.email.lock(self._principal),
            uow.people.lock(self._principal),
        ):
            for _ in range(10):
                rows = await uow.people.query(query)
                for row in rows[:100]:
                    if len(work) == limit:
                        return work
                    if isinstance(row, PeopleInteraction) and summary_pending(row):
                        item = await self._summary_work(uow, row, accounts)
                        if item is not None:
                            work.append(item)
                if len(rows) <= 100:
                    break
                last = rows[99]
                query = query.model_copy(
                    update={
                        "after": last.id,
                        "after_event_at": last.occurred_at
                        if isinstance(last, PeopleInteraction)
                        else None,
                    }
                )
        return work

    async def _summary_work(
        self, uow: RepositoryUnitOfWork, row: PeopleInteraction, accounts: set[str]
    ) -> CorrespondenceSummaryWork | None:
        examined = unavailable = 0
        for source_id in row.support_ids:
            source = await uow.people.get(
                self._principal, source_id, ceiling=Sensitivity.RESTRICTED
            )
            if not isinstance(source, PeopleSource) or source.source_kind != "email":
                continue
            examined += 1
            if (
                source.account_id not in accounts
                or not source.thread_id
                or not source.message_id
                or await uow.people.source_suppressed(self._principal, source.id)
            ):
                continue
            if await self.bulk_source(uow, source.account_id, source.thread_id):
                unavailable += 1
                continue
            record = await uow.email.get(
                self._principal,
                "semantic_source",
                semantic_source_key(source.account_id, source.thread_id, source.message_id),
            )
            try:
                if record is None:
                    raise ToolTrustRejectedError("email source is not retained")
                passages = self._retained_passages(record)
                offsets = sorted(int(key) for key in passages if str(key).isdigit())
                if not offsets:
                    raise ToolTrustRejectedError("email source has no retained passage")
                verified, header, _event = await self._verify_passage(
                    uow, self._pending_source(record, offsets[0]), passages
                )
            except (ToolTrustRejectedError, ConflictError, NotFoundError):
                unavailable += 1
                continue
            assert row.direction in {"incoming", "outgoing"}
            return CorrespondenceSummaryWork(
                interaction_id=row.id,
                source_id=source.id,
                account_id=source.account_id,
                provider_thread_id=source.thread_id,
                message_id=source.message_id,
                direction="outgoing" if row.direction == "outgoing" else "incoming",
                sender=verified.sender,
                to=header_text(header.get("to")),
                cc=header_text(header.get("cc")),
                subject=header_text(header.get("subject"), limit=998) or "",
                sent_at=verified.sent_at,
                passage=verified.body,
                passage_sha256=hashlib.sha256(verified.body.encode()).hexdigest(),
                retrying=row.summary_provenance is not None
                and row.summary_provenance.state == "retry",
            )
        if examined and unavailable == examined:
            # No account can ever supply this passage again: abstain for good.
            await self._put_summary(
                uow,
                row,
                summary=observed_email_label(row.direction),
                provenance=InteractionSummaryProvenance(
                    state="abstained",
                    generator=CORRESPONDENCE_SUMMARY_GENERATOR,
                    recorded_at=self._clock.now(),
                ),
            )
        return None

    async def record_correspondence_summary(
        self,
        work: CorrespondenceSummaryWork,
        result: EmailCorrespondenceSummary | None,
        *,
        model: str,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> str:
        """Store a valid gist, or count an invalid one: one retry, then abstain."""
        if not self.enabled:
            return "skipped"
        text = summary_text(result, work)
        async with (
            self._uow_factory() as uow,
            uow.email.lock(self._principal),
            uow.people.lock(self._principal),
        ):
            current = await uow.people.get(
                self._principal, work.interaction_id, ceiling=Sensitivity.RESTRICTED
            )
            if (
                not isinstance(current, PeopleInteraction)
                or not summary_pending(current)
                or work.source_id not in current.support_ids
                or await uow.people.source_suppressed(self._principal, work.source_id)
                or await self.bulk_source(uow, work.account_id, work.provider_thread_id)
            ):
                return "skipped"
            record = await uow.email.get(
                self._principal,
                "semantic_source",
                semantic_source_key(work.account_id, work.provider_thread_id, work.message_id),
            )
            passages = None if record is None else record.payload.get("passages")
            if (
                record is None
                or record.payload.get("excluded")
                or not isinstance(passages, dict)
                or work.passage_sha256 not in passages.values()
            ):
                return "skipped"
            label = observed_email_label(current.direction)
            retried = (
                current.summary_provenance is not None
                and current.summary_provenance.state == "retry"
            )
            state: Literal["generated", "retry", "abstained"] = (
                "generated" if text else "abstained" if retried else "retry"
            )
            if run is not None:
                # The lease fences a worker that no longer owns this refresh.
                await uow.events.append(
                    NewEvent(
                        session_id=run.session_id,
                        run_id=run.id,
                        event_type="email.correspondence.summarized",
                        actor_type="runtime",
                        payload={
                            "interaction_id": str(current.id),
                            "source_id": str(work.source_id),
                            "state": state,
                            "generator": CORRESPONDENCE_SUMMARY_GENERATOR,
                        },
                    ),
                    lease=lease,
                )
            await self._put_summary(
                uow,
                current,
                summary=f"{label}: {text}" if text else label,
                provenance=InteractionSummaryProvenance(
                    state=state,
                    generator=CORRESPONDENCE_SUMMARY_GENERATOR,
                    source_id=work.source_id,
                    passage_sha256=work.passage_sha256,
                    model=model,
                    recorded_at=self._clock.now(),
                ),
            )
            return state

    async def _put_summary(
        self,
        uow: RepositoryUnitOfWork,
        row: PeopleInteraction,
        *,
        summary: str,
        provenance: InteractionSummaryProvenance,
    ) -> None:
        now = self._clock.now()
        await uow.people.put(
            row.model_copy(
                update={
                    "summary": summary,
                    "summary_provenance": provenance,
                    "revision": row.revision + 1,
                    "updated_at": max(now, row.updated_at + timedelta(microseconds=1)),
                }
            ),
            expected_revision=row.revision,
        )

    async def retained_message(
        self, account_id: str, thread_id: str, message_id: str, *, offset: int = 0
    ) -> RetainedEmailMessage | None:
        """A bounded window of the original text Veetbot kept when it read the mail.

        Each passage is verified against its first-party read event and stored
        digest. The window stops at a gap between retained passages. Excluded,
        bulk, suppressed or unverifiable sources return nothing.
        """
        if not self.enabled or offset < 0:
            return None
        async with self._uow_factory() as uow:
            record = await uow.email.get(
                self._principal,
                "semantic_source",
                semantic_source_key(account_id, thread_id, message_id),
            )
            try:
                if record is None or await self.bulk_source(uow, account_id, thread_id):
                    return None
                passages = self._retained_passages(record)
                offsets = sorted(int(key) for key in passages if str(key).isdigit())
                parts: list[str] = []
                first: tuple[EmailSemanticSource, dict[str, object]] | None = None
                ends_message = exhausted = False
                expected = offsets[0] if offsets else 0
                for index, byte_offset in enumerate(offsets):
                    if byte_offset != expected:
                        break
                    source, header, _event = await self._verify_passage(
                        uow, self._pending_source(record, byte_offset), passages
                    )
                    if await uow.people.source_suppressed(
                        self._principal, email_source_id(self._principal, source)
                    ):
                        return None
                    first = first or (source, header)
                    parts.append(source.body)
                    expected = byte_offset + len(source.body.encode())
                    ends_message = await self._passage_ends_message(uow, source, header)
                    exhausted = index == len(offsets) - 1
                    if sum(len(part) for part in parts) > offset + RETAINED_TEXT_WINDOW:
                        break
            except (ToolTrustRejectedError, ConflictError, NotFoundError):
                return None
        text = "".join(parts)
        if first is None or offset >= len(text):
            return None
        window = text[offset : offset + RETAINED_TEXT_WINDOW]
        end = offset + len(window)
        source, header = first
        return RetainedEmailMessage(
            account_id=account_id,
            provider_thread_id=thread_id,
            message_id=message_id,
            sender=source.sender,
            to=header_text(header.get("to")),
            cc=header_text(header.get("cc")),
            subject=header_text(header.get("subject"), limit=998) or "",
            sent_at=source.sent_at,
            text=window,
            offset=offset,
            next_offset=end if end < len(text) else None,
            # Complete only when the retained text runs from the first byte to the end.
            complete=exhausted
            and ends_message
            and end == len(text)
            and bool(offsets)
            and offsets[0] == 0,
        )

    async def _passage_ends_message(
        self, uow: RepositoryUnitOfWork, source: EmailSemanticSource, header: dict[str, object]
    ) -> bool:
        """Whether this verified passage is the last part of the message body."""
        if not source.tool_name.endswith(".get_message_body"):
            return header.get("body_complete") is True
        document, _event = await self._read_document(
            uow, source, source.source_event_sequence, source.tool_name
        )
        return document.get("complete") is True

    def _retained_passages(self, record: EmailRecord) -> dict[str, object]:
        if (
            record.tenant_id != self._principal.tenant_id
            or record.principal_id != self._principal.principal_id
            or record.kind != "semantic_source"
            or record.payload.get("excluded")
        ):
            raise ToolTrustRejectedError("email import source is unavailable")
        passages = record.payload.get("passages")
        if not isinstance(passages, dict) or not isinstance(
            record.payload.get("occurrences"), list
        ):
            raise ToolTrustRejectedError("email import lacks retained passage provenance")
        return passages

    def _pending_source(self, record: EmailRecord, offset: int) -> EmailSemanticSource:
        """The passage's provenance before its original events are read and verified."""
        payload = record.payload
        occurrences = payload.get("occurrences")
        occurrence = next(
            (
                item
                for item in (occurrences if isinstance(occurrences, list) else [])
                if isinstance(item, dict) and item.get("body_offset") == offset
            ),
            None,
        )
        if occurrence is None:
            raise ToolTrustRejectedError("email import passage lacks its original event")
        return EmailSemanticSource.model_validate(
            {
                "account_id": payload.get("account_id"),
                "provider_thread_id": payload.get("provider_thread_id"),
                "message_id": payload.get("message_id"),
                "sent_at": payload.get("evidence_at"),
                "session_id": occurrence.get("session_id"),
                "source_event_sequence": occurrence.get("event_sequence"),
                "header_event_sequence": occurrence.get("header_event_sequence"),
                "header_session_id": occurrence.get("header_session_id"),
                "tool_name": occurrence.get("tool_name"),
                "body_offset": offset,
                "body": "pending verification",
                "sender": "pending verification",
            }
        )

    async def _verify_passage(
        self,
        uow: RepositoryUnitOfWork,
        source: EmailSemanticSource,
        passages: dict[str, object],
    ) -> tuple[EmailSemanticSource, dict[str, object], EventEnvelope]:
        """Read the original events and return the verified source, header and header event."""
        body, event = await self._read_document(
            uow, source, source.source_event_sequence, source.tool_name
        )
        header = body
        if source.tool_name.endswith(".get_message_body"):
            if source.header_event_sequence is None:
                raise ToolTrustRejectedError("email import lacks original headers")
            header, event = await self._read_document(
                uow,
                source,
                source.header_event_sequence,
                source.tool_name.rsplit(".", 1)[0] + ".get_thread_page",
                session_id=source.header_session_id,
            )
        messages = header.get("messages")
        matches = (
            [
                item
                for item in messages
                if isinstance(item, dict) and item.get("id") == source.message_id
            ]
            if isinstance(messages, list)
            else []
        )
        if len(matches) != 1:
            raise ToolTrustRejectedError("email import header is missing or ambiguous")
        message = matches[0]
        source = EmailSemanticSource.model_validate(
            {
                **source.model_dump(),
                "sender": message.get("from"),
                "body": body.get("body")
                if source.tool_name.endswith(".get_message_body")
                else message.get("body"),
            }
        )
        await self._validate_source(uow, source)
        if hashlib.sha256(source.body.encode()).hexdigest() != passages[str(source.body_offset)]:
            raise ToolTrustRejectedError("email import passage fingerprint changed")
        return source, message, event

    async def retain_source(
        self, source: EmailSemanticSource, *, run: Run, lease: WorkerLease | None
    ) -> None:
        """Retain verified provenance before chronological analysis, without projections."""
        if not self.enabled or self._import_window is None:
            raise ConflictError("historical email capture is unavailable")
        async with (
            self._uow_factory() as uow,
            uow.email.lock(self._principal),
            uow.people.lock(self._principal),
        ):
            await self._admit_import(uow, source)
            await self._validate_source(uow, source)
            await self._guard_run(uow, source, run, lease)
            if await uow.people.source_suppressed(
                self._principal, email_source_id(self._principal, source)
            ):
                raise ConflictError("People email source was erased")
            await self._register(uow, source)

    async def register_source(
        self,
        source: EmailSemanticSource,
        *,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> None:
        if not self.enabled or (
            self._import_window is None and source.sent_at < self._clock.now() - timedelta(days=90)
        ):
            await super().register_source(source, run=run, lease=lease)
            return
        async with (
            self._uow_factory() as uow,
            uow.email.lock(self._principal),
            uow.people.lock(self._principal),
        ):
            await self._admit_import(uow, source)
            await self._validate_source(uow, source)
            await self._guard_run(uow, source, run, lease)
            await self._register(uow, source)
            header_sequence = source.header_event_sequence or source.source_event_sequence
            header_tool = source.tool_name.rsplit(".", 1)[0] + ".get_thread_page"
            document, event = await self._read_document(
                uow, source, header_sequence, header_tool, session_id=source.header_session_id
            )
            messages = document.get("messages", [])
            header = (
                next(
                    (
                        item
                        for item in messages
                        if isinstance(item, dict) and item.get("id") == source.message_id
                    ),
                    None,
                )
                if isinstance(messages, list)
                else None
            )
            if header is not None:
                await project_correspondence(
                    uow, self._principal, source, header, event, self._clock.now()
                )

    async def _prepare_fact_people(
        self,
        uow: RepositoryUnitOfWork,
        fact: EmailPeopleFact,
        source: EmailSemanticSource,
        event: EventEnvelope,
        admitted: FormationSource,
        scope: str,
        self_references: frozenset[str],
    ) -> PreparedPeople:
        """Ground one fact's People evidence; mail never creates a person (ADR-0121)."""
        assert fact.people is not None
        if source.body.count(fact.quote) != 1:
            raise ToolValidationError("People email quote is ambiguous within this passage")
        offset = source.body.index(fact.quote)
        payload = fact.people.model_dump()
        for mention in [*payload["mentions"], *payload["organizations"]]:
            mention.update(
                source_event_id=event.sequence,
                start=mention["start"] + offset,
                end=mention["end"] + offset,
            )
        if payload["commitment"] is not None:
            payload["commitment"]["source_event_id"] = event.sequence
        candidate = MemoryCandidate(
            belief_type=fact.belief_type,
            subject=fact.subject,
            statement=fact.quote,
            source_event_ids=[event.sequence],
            model_confidence=0.4,
            proposed_scope=scope,
            proposed_portability=Portability.CONTEXTUAL,
            sensitivity_guess=Sensitivity.SENSITIVE,
            derivation=MemoryDerivation.HYPOTHESIS,
            longevity=MemoryLongevity.TENTATIVE,
            people=PeopleClaim.model_validate(payload),
        )
        # Names in a message body link only to people already in People; the
        # owner's own correspondence, not a mention, is what adds someone.
        return await prepare_people(
            uow.people,
            self._principal,
            candidate,
            {event.sequence: admitted},
            self._clock.now(),
            email=source,
            creatable=frozenset(),
            self_references=self_references,
        )

    async def form(
        self,
        source: EmailSemanticSource,
        facts: Sequence[EmailSemanticFact],
        *,
        scope: str = "general",
        run_id: UUID | None = None,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> list[MemoryRecord]:
        if not self.enabled:
            return []
        if (
            len(facts) > 20
            or sum(
                len(fact.people.mentions) + len(fact.people.organizations)
                for fact in facts
                if isinstance(fact, EmailPeopleFact) and fact.people is not None
            )
            > 64
        ):
            raise ToolValidationError("email People proposal exceeds its shared limit")
        if self._import_window is None and source.sent_at < self._clock.now() - timedelta(days=90):
            return []
        async with (
            self._uow_factory() as uow,
            uow.email.lock(self._principal),
            uow.people.lock(self._principal),
        ):
            await self._admit_import(uow, source)
            await self._validate_source(uow, source)
            await self._guard_run(uow, source, run, lease)
            if run is not None:
                if run_id is not None and run_id != run.id:
                    raise ToolValidationError("semantic run identity differs from its audit")
                run_id = run.id if run.session_id == source.session_id else None
            if any(
                isinstance(fact, EmailPeopleFact)
                and fact.people is not None
                and fact.people.commitment is not None
                and fact.people.commitment.state not in {"proposed", "uncertain"}
                for fact in facts
            ):
                header_document, _ = await self._read_document(
                    uow,
                    source,
                    source.header_event_sequence or source.source_event_sequence,
                    source.tool_name.rsplit(".", 1)[0] + ".get_thread_page",
                    session_id=source.header_session_id,
                )
                headers = header_document.get("messages", [])
                if not isinstance(headers, list):
                    raise ToolValidationError("email source headers are invalid")
                if any(
                    isinstance(header, dict)
                    and header.get("id") == source.message_id
                    and isinstance(header.get("label_ids"), list)
                    and "DRAFT" in header["label_ids"]
                    for header in headers
                ):
                    raise ToolValidationError("email draft cannot establish a committed action")
            sid = email_source_id(self._principal, source)
            if await uow.people.source_suppressed(self._principal, sid):
                raise ConflictError("People email source was erased")
            registered = await self._register(uow, source)
            event = (
                await uow.events.list_after(
                    source.session_id, source.source_event_sequence - 1, self._principal, limit=1
                )
            )[0]
            admitted = FormationSource(
                event=event,
                kind=FormationSourceKind.ATTRIBUTED_COMMUNICATION,
                channel="email",
                text=source.body,
            )
            prepared: list[tuple[EmailSemanticFact, PreparedPeople | None]] = []
            self_references = await owner_references(uow.email, self._principal)
            for proposed in facts:
                fact = (
                    EmailPeopleFact.model_validate(proposed.model_dump())
                    if isinstance(proposed, EmailPeopleFact)
                    else EmailSemanticFact.model_validate(proposed.model_dump())
                )
                if (
                    fact.message_id != source.message_id
                    or fact.quote not in source.body
                    or fact.value not in fact.quote
                ):
                    raise ToolValidationError("semantic email fact lacks exact source grounding")
                linked = None
                if isinstance(fact, EmailPeopleFact) and fact.people is not None:
                    try:
                        linked = await self._prepare_fact_people(
                            uow, fact, source, event, admitted, scope, self_references
                        )
                    except (ValueError, ToolValidationError):
                        # Unsupported People evidence drops the link, never the fact,
                        # as chat formation already does.
                        linked = None
                    except ConflictError:
                        # An erased source or identity rejects only this fact.
                        continue
                prepared.append((fact, linked))
            prior_facts = registered.payload.get("facts", {})
            fact_ids = dict(prior_facts) if isinstance(prior_facts, dict) else {}
            formed = []
            for fact, linked in prepared:
                key = hashlib.sha256(
                    json.dumps(
                        [EMAIL_PEOPLE_POLICY, fact.model_dump(mode="json", exclude={"confidence"})],
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                if key in fact_ids:
                    try:
                        prior = await uow.memories.get(UUID(str(fact_ids[key])), self._principal)
                    except NotFoundError:
                        continue
                    formed.append(prior)
                    continue
                statement = (
                    f"Email from {source.sender} on {source.sent_at.date().isoformat()} "
                    f"(mailbox {source.account_id}, thread {source.provider_thread_id}, "
                    f"message {source.message_id}) reports {fact.subject} "
                    f'{fact.predicate}: "{fact.value}".'
                )
                belief, _ = await self._governed.remember_formation(
                    session_id=source.session_id,
                    run_id=run_id,
                    statement=statement,
                    subject=linked.subject if linked else fact.subject,
                    scope=scope,
                    belief_type=fact.belief_type,
                    portability=Portability.CONTEXTUAL,
                    sensitivity=Sensitivity.SENSITIVE,
                    source_event_ids=[event.sequence],
                    origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
                    explicit=False,
                    authority=MemoryAuthority.INFERRED,
                    polarity=Polarity.ASSERT,
                    confidence=min(fact.confidence, 0.4),
                    valid_from=source.sent_at,
                    expires_at=source.sent_at + timedelta(days=30),
                    trigger=EMAIL_PEOPLE_POLICY,
                    record_audit=True,
                    derivation=MemoryDerivation.HYPOTHESIS,
                    longevity=MemoryLongevity.TENTATIVE,
                    evidence_at=source.sent_at,
                    attributed_external=True,
                    semantic_external=True,
                    existing_uow=uow,
                    audit_model=f"{self._provider}/{self._model}",
                )
                if linked is not None:
                    await persist_people(
                        uow.people, self._principal, linked, belief, self._clock.now()
                    )
                formed.append(belief)
                fact_ids[key] = str(belief.id)
            previous_ids = registered.payload.get("memory_ids", [])
            memory_ids = (
                sorted({str(value) for value in previous_ids} | {str(row.id) for row in formed})
                if isinstance(previous_ids, list)
                else [str(row.id) for row in formed]
            )
            if fact_ids != prior_facts:
                await uow.email.put(
                    registered.model_copy(
                        update={
                            "revision": registered.revision + 1,
                            "updated_at": self._clock.now(),
                            "payload": {
                                **registered.payload,
                                "facts": fact_ids,
                                "memory_ids": memory_ids,
                            },
                        }
                    ),
                    expected_revision=registered.revision,
                )
            return formed
