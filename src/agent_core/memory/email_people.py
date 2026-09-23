"""Separately evaluated People formation from verified email source passages."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, timedelta
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailRecord
from agent_core.domain.email_people import EmailPeopleFact
from agent_core.domain.email_people_evidence import EmailPeopleEvidence
from agent_core.domain.email_semantics import EmailSemanticFact, EmailSemanticSource
from agent_core.domain.errors import (
    ConflictError,
    NotFoundError,
    ToolTrustRejectedError,
    ToolValidationError,
)
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
        if (
            record.tenant_id != self._principal.tenant_id
            or record.principal_id != self._principal.principal_id
            or record.kind != "semantic_source"
            or record.payload.get("excluded")
        ):
            raise ToolTrustRejectedError("email import source is unavailable")
        payload = record.payload
        passages = payload.get("passages")
        occurrences = payload.get("occurrences")
        if not isinstance(passages, dict) or not isinstance(occurrences, list):
            raise ToolTrustRejectedError("email import lacks retained passage provenance")
        offsets = sorted(int(key) for key in passages if str(key).isdigit())
        offset = next((key for key in offsets if after_offset is None or key > after_offset), None)
        if offset is None:
            return None
        occurrence = next(
            (
                item
                for item in occurrences
                if isinstance(item, dict) and item.get("body_offset") == offset
            ),
            None,
        )
        if occurrence is None:
            raise ToolTrustRejectedError("email import passage lacks its original event")
        source = EmailSemanticSource.model_validate(
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
        async with (
            self._uow_factory() as uow,
            uow.email.lock(self._principal),
            uow.people.lock(self._principal),
        ):
            await self._admit_import(uow, source)
            current = await uow.email.get(self._principal, record.kind, record.key)
            if current is None or current.payload.get("excluded"):
                raise ConflictError("email import source was excluded")
            if await self.bulk_indexed(uow, source.account_id, source.provider_thread_id):
                # ADR-0116: a retained passage from a sender the census now indexes
                # is skipped and counted as excluded rather than analyzed.
                return None
            body, _ = await self._read_document(
                uow, source, source.source_event_sequence, source.tool_name
            )
            header = body
            if source.tool_name.endswith(".get_message_body"):
                if source.header_event_sequence is None:
                    raise ToolTrustRejectedError("email import lacks original headers")
                header, _ = await self._read_document(
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
            if hashlib.sha256(source.body.encode()).hexdigest() != passages[str(offset)]:
                raise ToolTrustRejectedError("email import passage fingerprint changed")
            if await uow.people.source_suppressed(
                self._principal, email_source_id(self._principal, source)
            ):
                raise ConflictError("People email source was erased")
            return source

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
                    if source.body.count(fact.quote) != 1:
                        raise ToolValidationError(
                            "People email quote is ambiguous within this passage"
                        )
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
                    linked = await prepare_people(
                        uow.people,
                        self._principal,
                        candidate,
                        {event.sequence: admitted},
                        self._clock.now(),
                        email=source,
                    )
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
