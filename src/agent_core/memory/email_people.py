"""Separately evaluated People formation from verified email source passages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
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
from agent_core.domain.email import (
    EMAIL_HISTORY_DAYS,
    EmailAccount,
    EmailFeedback,
    EmailMessage,
    EmailRecord,
    EmailThread,
)
from agent_core.domain.email_memory import (
    EMAIL_RETENTION_POLICY,
    EMAIL_STANDING_POLICY,
    FIRST_CLASS_CONFIDENCE_CEILING,
    GUESS_CONFIDENCE_CEILING,
    UNRATED,
    EmailMemoryStanding,
    EmailMergeReport,
    EmailRerateReport,
    attributed_email_statement,
    claim_key,
    claim_text,
    first_class_statement,
    guess_statement,
    kept_label,
    quoted_text,
    restated,
    stated_value,
    thread_importance,
    value_key,
)
from agent_core.domain.email_people import EmailPeopleFact
from agent_core.domain.email_people_evidence import EmailPeopleEvidence
from agent_core.domain.email_semantics import (
    EmailSemanticFact,
    EmailSemanticSource,
    semantic_source_key,
    thread_source_key,
)
from agent_core.domain.errors import (
    ConflictError,
    NotFoundError,
    ToolTrustRejectedError,
    ToolValidationError,
)
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import (
    LIVE_MEMORY_STATUSES,
    MemoryAuthority,
    MemoryCandidate,
    MemoryDerivation,
    MemoryLongevity,
    MemoryRecord,
    MemoryStatus,
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
    Person,
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
from agent_core.memory.people import resolve_identity
from agent_core.memory.people_correspondence import _addresses, project_correspondence
from agent_core.memory.people_formation import (
    PreparedPeople,
    email_source_id,
    owner_references,
    persist_people,
    prepare_people,
)
from agent_core.memory.predicates import predicate_key
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

EMAIL_PEOPLE_POLICY = EMAIL_STANDING_POLICY
# Sources one re-rating transaction examines before it commits and continues.
_RERATE_PAGE = 200
# What a retained message did for a memory it lists, strongest first. The source
# record's `facts` map keeps one `<role>:<memory id>` entry beside each fact's
# identity, so a message is told apart by itself and not by its timestamp.
_FORMED, _RENEWED, _SEEN = "formed", "renewed", "seen"
# What stored state says of a message's sender. Unverified is an address the
# owner knows on a received message Gmail gave no verdict for: it earns no
# standing, and takes none away from a memory that already has it.
_KNOWN, _UNVERIFIED, _UNKNOWN = "known", "unverified", "unknown"


def _source_role(facts: object, memory_id: UUID | str) -> str | None:
    """What this retained message did for a memory, or None on a record that predates roles."""
    if not isinstance(facts, dict):
        return None
    return next(
        (role for role in (_FORMED, _RENEWED, _SEEN) if f"{role}:{memory_id}" in facts), None
    )


def _note_role(
    facts: dict[str, object], listed_before: frozenset[str], memory_id: UUID, renewed: bool
) -> None:
    """Record that this message renewed, or only found, a memory another message formed.

    A stronger role already recorded stands. A memory the record listed before
    roles were kept stays unmarked unless this message has now renewed it, so
    its dates go on answering for it.
    """
    current = _source_role(facts, memory_id)
    if renewed and current != _FORMED:
        facts[f"{_RENEWED}:{memory_id}"] = str(memory_id)
    elif not renewed and current is None and str(memory_id) not in listed_before:
        facts[f"{_SEEN}:{memory_id}"] = str(memory_id)


class _Rerating:
    """One pass's stored-state reads and its per-memory outcomes (ADR-0340)."""

    def __init__(self) -> None:
        self.changed: set[UUID] = set()
        self.promoted: set[UUID] = set()
        self.demoted: set[UUID] = set()
        self.first_class: dict[UUID, bool] = {}
        self.retention: dict[UUID, str] = {}
        # A preview writes nothing, so it keeps each memory as an earlier visit
        # would have left it, for the later visit that an applied pass reads back.
        self.projected: dict[UUID, MemoryRecord] = {}
        self.fresh()

    def fresh(self) -> None:
        """Forget what was read from the store; the outcomes stay.

        The reads hold only inside one transaction, under its mailbox and
        People locks. The owner may change feedback, a thread or a sender
        between two transactions of the same pass.
        """
        self.feedback: list[EmailFeedback] | None = None
        self.owned: frozenset[str] | None = None
        self.threads: dict[str, EmailThread | None] = {}
        self.ratings: dict[str, tuple[float | None, bool, bool]] = {}
        self.known: dict[tuple[str, str, str], str] = {}

    def report(self, *, applied: bool) -> EmailRerateReport:
        by_retention: dict[str, int] = {}
        for label in self.retention.values():
            by_retention[label] = by_retention.get(label, 0) + 1
        return EmailRerateReport(
            applied=applied,
            examined=len(self.first_class),
            changed=len(self.changed),
            promoted=len(self.promoted),
            demoted=len(self.demoted),
            first_class=sum(self.first_class.values()),
            guesses=sum(not value for value in self.first_class.values()),
            by_retention=by_retention,
        )


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

    async def _standing(
        self,
        uow: RepositoryUnitOfWork,
        account_id: str,
        provider_thread_id: str,
        message_id: str,
        *,
        sender: str | None = None,
        seen: _Rerating | None = None,
    ) -> EmailMemoryStanding:
        """What stored state says about this message's thread and sender (ADR-0340).

        Everything is read from the mailbox projection and People, never from
        the caller. A thread with no stored record is unrated and its sender
        unknown, which is today's thirty-day guess.
        """
        seen = seen or _Rerating()
        source_key = thread_source_key(account_id, provider_thread_id)
        await self._stored_thread(uow, account_id, provider_thread_id, seen)
        thread = seen.threads[source_key]
        if thread is None:
            return UNRATED
        if seen.feedback is None:
            seen.feedback = []
            after: str | None = None
            while page := await uow.email.list(self._principal, "feedback", after=after):
                seen.feedback.extend(EmailFeedback.model_validate(row.payload) for row in page)
                after = page[-1].key
        if source_key not in seen.ratings:
            # The last assessment holds the rating a new message reset, and says
            # whether it read the thread or abstained.
            row = await uow.email.get(self._principal, "assessment", str(thread.id))
            seen.ratings[source_key] = thread_importance(
                thread, seen.feedback, None if row is None else row.payload
            )
        importance, marked, rating_known = seen.ratings[source_key]
        message = next((item for item in thread.messages if item.id == message_id), None)
        sender_seen = message is not None and (sender is None or message.sender == sender)
        known = _UNKNOWN
        if message is not None and sender_seen:
            known_key = (account_id, provider_thread_id, message_id)
            if known_key not in seen.known:
                seen.known[known_key] = await self._known_sender(uow, message, seen)
            known = seen.known[known_key]
        return EmailMemoryStanding(
            importance=importance or 0.0,
            owner_marked_important=marked,
            owner_replied=any(item.direction == "sent" for item in thread.messages),
            known_sender=known == _KNOWN,
            sender_unverified=known == _UNVERIFIED,
            # A floor lengthens on what it shows and is no rating to shorten by.
            rated=importance is not None and rating_known,
            complete=thread.complete,
            sender_seen=sender_seen,
        )

    async def _known_sender(
        self, uow: RepositoryUnitOfWork, message: EmailMessage, seen: _Rerating
    ) -> str:
        """Whether the sender is the owner on their own sent mail, or one person the
        owner knows or writes to, on a received message whose sender check passed.

        Anyone can write a familiar address in a From line. Gmail's passed
        check (ADR-0165) is what ties a received message to the address, as it
        is for a signature (ADR-0336). Without a verdict the sender is
        unverified: not known, and not shown to be anyone else either.
        """
        if message.sender_check == "fail":
            # An address that failed its check proves nothing about who wrote.
            return _UNKNOWN
        senders = _addresses(message.sender)
        if len(senders) != 1:
            return _UNKNOWN
        address = senders[0][1]
        if seen.owned is None:
            accounts = await uow.email.list(self._principal, "account", limit=101)
            seen.owned = frozenset(
                owned.casefold()
                for row in accounts
                for account in [EmailAccount.model_validate(row.payload)]
                for _, owned in _addresses([account.email_address, *account.verified_addresses])
            )
        if address.casefold() in seen.owned:
            # The owner's own sent mail carries no verdict and needs none.
            return _KNOWN if message.direction == "sent" else _UNKNOWN
        if message.direction == "sent":
            return _UNKNOWN
        match = await resolve_identity(
            uow.people,
            self._principal,
            kind="email",
            namespace="owner",
            value=address,
            context="owner",
            at=self._clock.now(),
            ceiling=Sensitivity.SENSITIVE,
        )
        if match.status != "matched":
            return _UNKNOWN
        person = await uow.people.get(
            self._principal, match.person_ids[0], ceiling=Sensitivity.RESTRICTED
        )
        if not (isinstance(person, Person) and person.state in {"active", "provisional"}):
            return _UNKNOWN
        return _KNOWN if message.sender_check == "pass" else _UNVERIFIED

    async def _stored_thread(
        self, uow: RepositoryUnitOfWork, account_id: str, provider_thread_id: str, seen: _Rerating
    ) -> EmailThread | None:
        """The thread as the mailbox projection holds it now, or None when it holds none."""
        source_key = thread_source_key(account_id, provider_thread_id)
        if source_key not in seen.threads:
            index = await uow.email.get(self._principal, "thread_source", source_key)
            thread_id = None if index is None else index.payload.get("thread_id")
            row = (
                await uow.email.get(self._principal, "thread", thread_id)
                if isinstance(thread_id, str)
                else None
            )
            seen.threads[source_key] = (
                None if row is None else EmailThread.model_validate(row.payload)
            )
        return seen.threads[source_key]

    async def _formed_by_message(
        self,
        uow: RepositoryUnitOfWork,
        registered: EmailRecord,
        subject: str,
        fact: EmailSemanticFact,
    ) -> MemoryRecord | None:
        """The memory this message already formed for this fact, in any status (ADR-0341).

        A message assessed again proposes the same fact in other words. The
        subject, belief type and value say it is the same one, so the first
        memory stands, including one that has since expired or been merged away.
        """
        wanted, named = value_key(fact.value), " ".join(subject.casefold().split())
        listed = registered.payload.get("memory_ids")
        for value in listed if isinstance(listed, list) else []:
            try:
                memory = await uow.memories.get(UUID(str(value)), self._principal)
            except (NotFoundError, ValueError):
                continue
            stated = stated_value(memory.statement, memory.subject)
            if (
                stated is not None
                and memory.consolidation_policy_version == EMAIL_PEOPLE_POLICY
                and memory.belief_type is fact.belief_type
                and " ".join(memory.subject.casefold().split()) == named
                and value_key(stated) == wanted
            ):
                return memory
        return None

    def _governed_by_table(self, memory: MemoryRecord) -> bool:
        """A live email memory nothing of the owner's has taken over, and not yet expired."""
        return (
            memory.consolidation_policy_version == EMAIL_PEOPLE_POLICY
            and memory.authority is MemoryAuthority.INFERRED
            and memory.status in LIVE_MEMORY_STATUSES
            and memory.superseded_by is None
            and attributed_email_statement(memory.statement)
            and (memory.expires_at is None or memory.expires_at > self._clock.now())
        )

    async def _renew(
        self,
        uow: RepositoryUnitOfWork,
        source: EmailSemanticSource,
        subject: str,
        fact: EmailSemanticFact,
        standing: EmailMemoryStanding,
        run_id: UUID | None,
    ) -> tuple[MemoryRecord | None, bool]:
        """The memory a different, earlier message already formed for this exact claim,
        and whether this message renewed it.

        A strictly later message renews it: the evidence date moves and the
        expiry is recomputed and never shortened. A first-class memory becomes
        active when the later message is from a known sender too. Older or
        same-dated mail that is processed afterwards finds the memory and
        changes nothing.
        """
        # ADR-0341: the same subject, belief type and value is the same fact,
        # however the model worded the predicate this time.
        wanted = value_key(fact.value)
        related = await uow.memories.related(
            self._principal.tenant_id, self._principal.principal_id, subject, fact.belief_type
        )
        current = next(
            (
                row
                for row in sorted(related, key=lambda item: item.store_position, reverse=True)
                if self._governed_by_table(row)
                and row.polarity is Polarity.ASSERT
                and (stated := stated_value(row.statement, row.subject)) is not None
                and value_key(stated) == wanted
            ),
            None,
        )
        if current is None:
            return None, False
        if source.sent_at <= current.last_evidence_at:
            return current, False
        expiry = standing.expires_at(source.sent_at)
        if current.expires_at is None or (expiry is not None and expiry < current.expires_at):
            expiry = current.expires_at
        ceiling = (
            FIRST_CLASS_CONFIDENCE_CEILING
            if current.derivation is MemoryDerivation.DIRECT
            else GUESS_CONFIDENCE_CEILING
        )
        now = self._clock.now()
        # Only someone the owner knows settles a first-class memory. A stranger
        # sending a claim twice renews its dates and leaves it a provisional guess.
        settles = current.derivation is MemoryDerivation.DIRECT and standing.known_sender
        renewed = await uow.memories.reinforce(
            current.model_copy(
                update={
                    "last_evidence_at": source.sent_at,
                    "evidence_count": current.evidence_count + 1,
                    "corroboration_count": current.corroboration_count + int(settles),
                    "confidence": max(current.confidence, min(ceiling, current.confidence + 0.1))
                    if settles
                    else current.confidence,
                    "expires_at": expiry,
                    "status": MemoryStatus.ACTIVE if settles else current.status,
                    "lifecycle_policy_version": EMAIL_RETENTION_POLICY,
                    "last_reinforced_at": now,
                    "store_position": await uow.memories.next_position(),
                    "updated_at": now,
                },
                deep=True,
            )
        )
        await self._governed._append_event(
            uow, source.session_id, run_id, "memory.reinforced", renewed
        )
        return renewed, True

    async def _keep_by_table(
        self, uow: RepositoryUnitOfWork, memory: MemoryRecord, standing: EmailMemoryStanding
    ) -> MemoryRecord:
        """Give a newly formed memory the table's expiry and its lifecycle label.

        Formation fills a missing expiry with thirty days for a tentative claim,
        which the table's upper tiers must replace. Old mail may form a memory
        that is already past its expiry, so the date is set whatever it is.
        """
        expiry = standing.expires_at(memory.last_evidence_at)
        if (
            memory.consolidation_policy_version != EMAIL_PEOPLE_POLICY
            or memory.authority is not MemoryAuthority.INFERRED
            or memory.status not in LIVE_MEMORY_STATUSES
            or memory.superseded_by is not None
            or not attributed_email_statement(memory.statement)
            or (
                memory.expires_at == expiry
                and memory.lifecycle_policy_version == EMAIL_RETENTION_POLICY
            )
        ):
            return memory
        return await uow.memories.reinforce(
            memory.model_copy(
                update={"expires_at": expiry, "lifecycle_policy_version": EMAIL_RETENTION_POLICY},
                deep=True,
            )
        )

    async def _rerate_source(
        self,
        uow: RepositoryUnitOfWork,
        record: EmailRecord,
        seen: _Rerating,
        *,
        apply: bool,
        adopt: bool,
    ) -> list[UUID]:
        """Re-rate the memories one retained message governs; returns those it changed.

        A message governs a memory's retention when it supplied the memory's
        last evidence, and its standing and wording when it formed the memory.
        For a memory nothing renewed, that is the same message. The record
        says which it did for each memory it lists: a message that only found
        a memory governs nothing, even when it carries the same timestamp as
        the one that formed it. Dates order the evidence; they identify a
        message only on a record written before roles were kept. Only the
        owner's command adopts a memory formed before the table existed; the
        hooks follow memories already under it, so nothing is backfilled silently.
        """
        payload = record.payload
        account_id, thread_id, message_id = (
            payload.get("account_id"),
            payload.get("provider_thread_id"),
            payload.get("message_id"),
        )
        memory_ids = payload.get("memory_ids")
        try:
            evidence_at = datetime.fromisoformat(str(payload.get("evidence_at")))
        except ValueError:
            return []
        if (
            payload.get("excluded")
            or not isinstance(account_id, str)
            or not isinstance(thread_id, str)
            or not isinstance(message_id, str)
            or not isinstance(memory_ids, list)
        ):
            return []
        standing = await self._standing(uow, account_id, thread_id, message_id, seen=seen)
        changed: list[UUID] = []
        for value in memory_ids:
            try:
                memory = await uow.memories.get(UUID(str(value)), self._principal)
            except (NotFoundError, ValueError):
                continue
            # A preview reads back what its earlier visit would have written.
            memory = seen.projected.get(memory.id, memory)
            role = _source_role(payload.get("facts"), memory.id)
            latest = role in {None, _FORMED, _RENEWED} and memory.last_evidence_at == evidence_at
            origin = role == _FORMED or (role is None and memory.valid_from == evidence_at)
            if (
                not self._governed_by_table(memory)
                or not (latest or origin)
                or not (adopt or memory.lifecycle_policy_version == EMAIL_RETENTION_POLICY)
            ):
                continue
            update: dict[str, object] = {}
            first_class = memory.derivation is MemoryDerivation.DIRECT
            # A promotion needs only what is visible. A demotion needs evidence:
            # the sender is seen and is not someone the owner knows, or a
            # current, complete reading keeps the thread under a year. A reset
            # rating, a message that left the stored window, or a sender check
            # that merely reads `none` demotes nothing.
            restandable = (
                origin
                and standing.sender_seen
                and standing.first_class != first_class
                and (standing.first_class or standing.may_demote)
            )
            if restandable:
                statement = restated(
                    memory.statement,
                    account_id=account_id,
                    thread_id=thread_id,
                    message_id=message_id,
                    first_class=standing.first_class,
                )
                if statement is not None:
                    first_class = standing.first_class
                    update.update(
                        statement=statement,
                        predicate_key=predicate_key(statement, memory.belief_type),
                        derivation=MemoryDerivation.DIRECT
                        if first_class
                        else MemoryDerivation.HYPOTHESIS,
                        longevity=MemoryLongevity.DURABLE
                        if first_class
                        else MemoryLongevity.TENTATIVE,
                        # A conflict link still asks the owner which belief holds.
                        flagged_for_review=not first_class or bool(memory.conflicts_with),
                        confidence=memory.confidence
                        if first_class
                        else min(memory.confidence, GUESS_CONFIDENCE_CEILING),
                    )
                    (seen.promoted if first_class else seen.demoted).add(memory.id)
            if latest:
                expiry, current = standing.expires_at(memory.last_evidence_at), memory.expires_at
                raises = current is not None and (expiry is None or expiry > current)
                # Lengthening needs only what is visible; shortening needs a
                # current, complete reading of the thread.
                if expiry != current and (raises or standing.may_lower):
                    update["expires_at"] = expiry
                if memory.lifecycle_policy_version != EMAIL_RETENTION_POLICY:
                    update["lifecycle_policy_version"] = EMAIL_RETENTION_POLICY
                seen.retention[memory.id] = kept_label(
                    update.get("expires_at", current),  # type: ignore[arg-type]
                    memory.last_evidence_at,
                )
            seen.first_class[memory.id] = first_class
            if not update:
                continue
            seen.changed.add(memory.id)
            changed.append(memory.id)
            if apply:
                now = self._clock.now()
                if "statement" in update:
                    # A reworded belief is a change the next turn should see.
                    update["store_position"] = await uow.memories.next_position()
                await uow.memories.reinforce(
                    memory.model_copy(update={**update, "updated_at": now}, deep=True)
                )
            else:
                seen.projected[memory.id] = memory.model_copy(update=update, deep=True)
        return changed

    async def _audit_rerating(
        self,
        uow: RepositoryUnitOfWork,
        changed: list[UUID],
        *,
        cause: str,
        run: Run | None,
        lease: WorkerLease | None,
    ) -> None:
        """One content-free event per applied re-rating that changed something."""
        if run is None and lease is not None:
            raise ToolValidationError("semantic lease requires its owning run")
        if not changed:
            return
        session_id = run.session_id if run is not None else None
        if session_id is None:
            session_id = (await uow.memories.get(changed[0], self._principal)).source_session_id
        await uow.events.append(
            NewEvent(
                session_id=session_id,
                run_id=None if run is None else run.id,
                event_type="email.memory.rerated",
                actor_type="runtime" if run is not None else "memory",
                payload={
                    "cause": cause,
                    "policy_version": EMAIL_RETENTION_POLICY,
                    "belief_ids": sorted(str(value) for value in changed),
                },
            ),
            lease=lease,
        )

    async def rerate_thread(
        self,
        account_id: str,
        provider_thread_id: str,
        *,
        apply: bool = True,
        cause: str = "assessment",
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> EmailRerateReport:
        """Make one thread's live email memories follow its current importance and sender."""
        seen = _Rerating()
        if not self.enabled:
            return seen.report(applied=apply)
        async with (
            self._uow_factory() as uow,
            uow.email.lock(self._principal),
            uow.people.lock(self._principal),
        ):
            await self._standing(uow, account_id, provider_thread_id, "", seen=seen)
            source_key = thread_source_key(account_id, provider_thread_id)
            thread = seen.threads.get(source_key)
            # A long thread stores a window of its messages; the index remembers
            # every message ever observed, and each may have formed a memory.
            index = await uow.email.get(self._principal, "thread_source", source_key)
            observed = None if index is None else index.payload.get("observed_message_ids")
            message_ids = {
                *(str(value) for value in (observed if isinstance(observed, list) else [])),
                *(message.id for message in ([] if thread is None else thread.messages)),
            }
            changed: list[UUID] = []
            for message_id in sorted(message_ids) if thread is not None else []:
                record = await uow.email.get(
                    self._principal,
                    "semantic_source",
                    semantic_source_key(account_id, provider_thread_id, message_id),
                )
                if record is not None:
                    changed += await self._rerate_source(
                        uow, record, seen, apply=apply, adopt=False
                    )
            if apply:
                await self._audit_rerating(uow, changed, cause=cause, run=run, lease=lease)
        return seen.report(applied=apply)

    async def merge_duplicates(self, *, apply: bool) -> EmailMergeReport:
        """Keep one memory per fact in each thread; a preview unless `apply` (ADR-0341).

        Within one thread, live email memories that share a subject, a belief
        type and a value are one fact. The earliest evidence stays and each
        other copy is superseded by it. Copies under different subjects are
        counted and left: two people in one thread can each hold the same value.

        The memory kept takes the latest evidence date and the longest
        retention among its copies, so the fact does not expire while a copy's
        later evidence still holds. The message behind that evidence is linked
        to the memory kept, so a later re-rating still follows its thread.
        """
        report = EmailMergeReport(applied=apply)
        if not self.enabled:
            return report
        by_thread: dict[tuple[str, str], list[str]] = {}
        # The retained messages that list each memory, by record key.
        listing: dict[str, list[str]] = {}
        after: str | None = None
        while True:
            async with self._uow_factory() as uow:
                page = await uow.email.list(
                    self._principal, "semantic_source", after=after, limit=_RERATE_PAGE
                )
            for record in page:
                payload = record.payload
                account_id, thread_id = (
                    payload.get("account_id"),
                    payload.get("provider_thread_id"),
                )
                listed = payload.get("memory_ids")
                if (
                    not payload.get("excluded")
                    and isinstance(account_id, str)
                    and isinstance(thread_id, str)
                    and isinstance(listed, list)
                ):
                    by_thread.setdefault((account_id, thread_id), []).extend(map(str, listed))
                    for value in listed:
                        listing.setdefault(str(value), []).append(record.key)
            if len(page) < _RERATE_PAGE:
                break
            after = page[-1].key
        examined = facts = superseded = other_subjects = 0
        for _thread, listed_ids in sorted(by_thread.items()):
            async with (
                self._uow_factory() as uow,
                uow.email.lock(self._principal),
                uow.people.lock(self._principal),
            ):
                groups: dict[tuple[str, str, str], list[MemoryRecord]] = {}
                for value in dict.fromkeys(listed_ids):
                    try:
                        memory = await uow.memories.get(UUID(value), self._principal)
                    except (NotFoundError, ValueError):
                        continue
                    stated = stated_value(memory.statement, memory.subject)
                    if (
                        stated is None
                        or memory.polarity is not Polarity.ASSERT
                        or not self._governed_by_table(memory)
                    ):
                        continue
                    examined += 1
                    key = (
                        memory.belief_type.value,
                        value_key(stated),
                        " ".join(memory.subject.casefold().split()),
                    )
                    groups.setdefault(key, []).append(memory)
                subjects_by_value: dict[tuple[str, str], int] = {}
                for belief_type, stated_key, _subject in groups:
                    pair = (belief_type, stated_key)
                    subjects_by_value[pair] = subjects_by_value.get(pair, 0) + 1
                other_subjects += sum(count - 1 for count in subjects_by_value.values())
                for group in groups.values():
                    if len(group) < 2:
                        continue
                    kept, *copies = sorted(
                        group, key=lambda item: (item.valid_from, item.store_position)
                    )
                    facts += 1
                    superseded += len(copies)
                    if not apply:
                        continue
                    now = self._clock.now()
                    await self._keep_later_evidence(uow, kept, copies, listing, now)
                    for copy in copies:
                        await uow.memories.reinforce(
                            copy.model_copy(
                                update={
                                    "status": MemoryStatus.SUPERSEDED,
                                    "valid_to": now,
                                    "superseded_by": kept.id,
                                    "store_position": await uow.memories.next_position(),
                                    "updated_at": now,
                                },
                                deep=True,
                            )
                        )
                    await uow.events.append(
                        NewEvent(
                            session_id=kept.source_session_id,
                            run_id=None,
                            event_type="email.memory.merged",
                            actor_type="memory",
                            payload={
                                "kept": str(kept.id),
                                "superseded": sorted(str(copy.id) for copy in copies),
                            },
                        )
                    )
        return report.model_copy(
            update={
                "examined": examined,
                "facts": facts,
                "superseded": superseded,
                "other_subjects": other_subjects,
            }
        )

    async def _keep_later_evidence(
        self,
        uow: RepositoryUnitOfWork,
        kept: MemoryRecord,
        copies: Sequence[MemoryRecord],
        listing: dict[str, list[str]],
        now: datetime,
    ) -> None:
        """Give the memory a merge keeps what its copies know that it does not.

        A later message that restates a fact renews the one memory: the
        evidence date moves and the expiry is never shortened. A copy is that
        renewal stored as a second row, so the merge applies it now. The
        standing, the wording and the first evidence date stay the survivor's.
        """
        update: dict[str, object] = {}
        newest = max(copies, key=lambda item: item.last_evidence_at)
        if newest.last_evidence_at > kept.last_evidence_at:
            later = {
                copy.last_evidence_at
                for copy in copies
                if copy.last_evidence_at > kept.last_evidence_at
            }
            update.update(
                last_evidence_at=newest.last_evidence_at,
                evidence_count=kept.evidence_count + len(later),
            )
            await self._link_latest_source(uow, kept, newest, listing.get(str(newest.id), []))
        expiries = [row.expires_at for row in (kept, *copies)]
        dated = [expiry for expiry in expiries if expiry is not None]
        longest = None if len(dated) < len(expiries) else max(dated)
        if longest != kept.expires_at:
            update["expires_at"] = longest
        if update:
            await uow.memories.reinforce(
                kept.model_copy(update={**update, "updated_at": now}, deep=True)
            )

    async def _link_latest_source(
        self,
        uow: RepositoryUnitOfWork,
        kept: MemoryRecord,
        copy: MemoryRecord,
        record_keys: Sequence[str],
    ) -> None:
        """Let the message behind a merged copy's last evidence answer for the memory kept."""
        for key in record_keys:
            record = await uow.email.get(self._principal, "semantic_source", key)
            if record is None or record.payload.get("excluded"):
                continue
            payload = record.payload
            facts, listed = payload.get("facts"), payload.get("memory_ids")
            try:
                evidence_at = datetime.fromisoformat(str(payload.get("evidence_at")))
            except ValueError:
                continue
            if evidence_at != copy.last_evidence_at or _source_role(facts, copy.id) == _SEEN:
                continue
            roles = dict(facts) if isinstance(facts, dict) else {}
            roles[f"{_RENEWED}:{kept.id}"] = str(kept.id)
            memory_ids = {str(value) for value in (listed if isinstance(listed, list) else [])}
            await uow.email.put(
                record.model_copy(
                    update={
                        "revision": record.revision + 1,
                        "updated_at": self._clock.now(),
                        "payload": {
                            **payload,
                            "facts": roles,
                            "memory_ids": sorted(memory_ids | {str(kept.id)}),
                        },
                    }
                ),
                expected_revision=record.revision,
            )
            return

    async def rerate_all(self, *, apply: bool) -> EmailRerateReport:
        """Re-rate every live email memory; a preview unless `apply` (ADR-0340).

        Sources are read in key order, a bounded page per transaction, so the
        mailbox lock is never held across the whole store. Each page reads
        feedback, threads and senders again under its own locks: the owner may
        mark a thread between two pages, and a rating read for the first would
        undo on the second what that mark's own re-rating just did.
        """
        seen = _Rerating()
        if not self.enabled:
            return seen.report(applied=apply)
        after: str | None = None
        while True:
            async with (
                self._uow_factory() as uow,
                uow.email.lock(self._principal),
                uow.people.lock(self._principal),
            ):
                seen.fresh()
                page = await uow.email.list(
                    self._principal, "semantic_source", after=after, limit=_RERATE_PAGE
                )
                changed: list[UUID] = []
                for record in page:
                    changed += await self._rerate_source(uow, record, seen, apply=apply, adopt=True)
                if apply:
                    await self._audit_rerating(uow, changed, cause="command", run=None, lease=None)
            if len(page) < _RERATE_PAGE:
                break
            after = page[-1].key
        return seen.report(applied=apply)

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
            seen = _Rerating()
            thread = await self._stored_thread(
                uow, source.account_id, source.provider_thread_id, seen
            )
            earlier_bodies = [
                message.body
                for message in ([] if thread is None else thread.messages)
                if message.id != source.message_id
                and message.sent_at < source.sent_at
                and message.body
            ]
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
                if quoted_text(fact.quote, source.body, earlier_bodies):
                    # ADR-0341: a sentence a reply quotes is not the replier's statement.
                    continue
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
            listed = registered.payload.get("memory_ids")
            listed_before = frozenset(map(str, listed if isinstance(listed, list) else []))
            formed = []
            # A historical import keeps the thirty-day horizon from the source
            # date, so importing never makes an old claim current (gate 26).
            standing = (
                UNRATED
                if self._import_window is not None
                else await self._standing(
                    uow,
                    source.account_id,
                    source.provider_thread_id,
                    source.message_id,
                    sender=source.sender,
                    seen=seen,
                )
            )
            first_class = standing.first_class
            for fact, linked in prepared:
                key = hashlib.sha256(
                    json.dumps(
                        [EMAIL_PEOPLE_POLICY, fact.model_dump(mode="json", exclude={"confidence"})],
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                subject = linked.subject if linked else fact.subject
                # ADR-0341: one memory per fact per message, whatever the wording.
                identity = claim_key(fact.belief_type.value, subject, fact.value)
                known = fact_ids.get(key) or fact_ids.get(identity)
                if known is not None:
                    fact_ids[identity] = known
                    # A memory the owner deleted is gone and is not formed again.
                    with suppress(NotFoundError):
                        formed.append(await uow.memories.get(UUID(str(known)), self._principal))
                    continue
                existing = await self._formed_by_message(uow, registered, subject, fact)
                if existing is None:
                    existing, renewed = await self._renew(
                        uow, source, subject, fact, standing, run_id
                    )
                    if existing is not None:
                        # Another message formed it. This one renewed it, or only found it.
                        _note_role(fact_ids, listed_before, existing.id, renewed)
                if existing is not None:
                    formed.append(existing)
                    fact_ids[key] = fact_ids[identity] = str(existing.id)
                    continue
                claim = claim_text(fact.subject, fact.predicate, fact.value)
                sent_on = source.sent_at.date().isoformat()
                # ADR-0340: a known sender in an important thread states the claim
                # plainly; every other email memory keeps the record line.
                statement = (
                    first_class_statement(source.sender, sent_on, claim)
                    if first_class
                    else guess_statement(
                        source.sender,
                        sent_on,
                        source.account_id,
                        source.provider_thread_id,
                        source.message_id,
                        claim,
                    )
                )
                belief, outcome = await self._governed.remember_formation(
                    session_id=source.session_id,
                    run_id=run_id,
                    statement=statement,
                    subject=subject,
                    scope=scope,
                    belief_type=fact.belief_type,
                    portability=Portability.CONTEXTUAL,
                    sensitivity=Sensitivity.SENSITIVE,
                    source_event_ids=[event.sequence],
                    origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
                    explicit=False,
                    authority=MemoryAuthority.INFERRED,
                    polarity=Polarity.ASSERT,
                    confidence=min(
                        fact.confidence,
                        FIRST_CLASS_CONFIDENCE_CEILING if first_class else GUESS_CONFIDENCE_CEILING,
                    ),
                    valid_from=source.sent_at,
                    expires_at=standing.expires_at(source.sent_at),
                    trigger=EMAIL_PEOPLE_POLICY,
                    record_audit=True,
                    derivation=MemoryDerivation.DIRECT
                    if first_class
                    else MemoryDerivation.HYPOTHESIS,
                    longevity=MemoryLongevity.DURABLE if first_class else MemoryLongevity.TENTATIVE,
                    evidence_at=source.sent_at,
                    attributed_external=True,
                    semantic_external=True,
                    existing_uow=uow,
                    audit_model=f"{self._provider}/{self._model}",
                )
                belief = await self._keep_by_table(uow, belief, standing)
                if linked is not None:
                    await persist_people(
                        uow.people, self._principal, linked, belief, self._clock.now()
                    )
                formed.append(belief)
                fact_ids[key] = fact_ids[identity] = str(belief.id)
                if outcome in {"committed", "conflicted", "superseded"}:
                    fact_ids[f"{_FORMED}:{belief.id}"] = str(belief.id)
                else:
                    # Formation answered with a belief that was already stored.
                    _note_role(fact_ids, listed_before, belief.id, False)
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
