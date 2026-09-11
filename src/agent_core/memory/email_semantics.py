"""Versioned semantic email proposals; provider calls remain in governed runs."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from agent_core.domain.agents import Principal
from agent_core.domain.email import EmailRecord
from agent_core.domain.email_semantics import (
    EmailSemanticFact as EmailSemanticFact,
)
from agent_core.domain.email_semantics import (
    EmailSemanticProposal as EmailSemanticProposal,
)
from agent_core.domain.email_semantics import (
    EmailSemanticSource as EmailSemanticSource,
)
from agent_core.domain.email_semantics import (
    EmailSemanticValue,
)
from agent_core.domain.email_semantics import (
    semantic_source_key as semantic_source_key,
)
from agent_core.domain.errors import (
    ConflictError,
    NotFoundError,
    ToolTrustRejectedError,
    ToolValidationError,
)
from agent_core.domain.events import EventEnvelope, NewEvent
from agent_core.domain.memory import (
    BeliefRejection,
    MemoryAuthority,
    MemoryDerivation,
    MemoryLongevity,
    MemoryRecord,
    Polarity,
    Portability,
    RejectionKind,
    Sensitivity,
)
from agent_core.domain.messages import TextPart, ToolResultItem
from agent_core.domain.persistence import WorkerLease
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import Run
from agent_core.memory.formation import GovernedMemoryService
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

EMAIL_SEMANTIC_POLICY_VERSION = "email-semantic@1"


class EmailSemanticEvaluationEvidence(EmailSemanticValue):
    """Exact configuration evidence; no development fixture enables production."""

    policy_version: Literal["email-semantic@1"] = "email-semantic@1"
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    build_ref: str = Field(min_length=1)
    corpus_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    implementation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    sample_count: int = Field(ge=20)
    supported_count: int = Field(ge=20)
    labeled_useful_count: int = Field(ge=20)
    comparison_case_count: int = Field(ge=1)
    improvement_count: int = Field(ge=1)
    existing_memory_benchmarks_passed: Literal[True]
    attribution_case_count: int = Field(ge=1)
    cross_project_case_count: int = Field(ge=1)
    historical_case_count: int = Field(ge=1)
    fabricated_count: Literal[0] = 0
    authority_failure_count: Literal[0] = 0
    relevance_failure_count: Literal[0] = 0
    evaluated_at: datetime

    @model_validator(mode="after")
    def complete_evidence(self) -> EmailSemanticEvaluationEvidence:
        if self.supported_count > self.sample_count:
            raise ValueError("supported cases exceed sample count")
        if self.supported_count * 100 < self.sample_count * 95:
            raise ValueError("semantic precision is below 95 percent")
        if not self.supported_count <= self.labeled_useful_count:
            raise ValueError("supported cases exceed labeled useful facts")
        if self.supported_count * 100 < self.labeled_useful_count * 85:
            raise ValueError("semantic recall is below 85 percent")
        if self.improvement_count > self.comparison_case_count:
            raise ValueError("improvements exceed baseline comparisons")
        if self.evaluated_at.tzinfo is None or self.evaluated_at.utcoffset() is None:
            raise ValueError("evaluation time must be timezone-aware")
        return self


def _items(value: object) -> list[object]:
    return list(value) if isinstance(value, list) else []


class EmailSemanticFormationService:
    """Admit proposals made by ordinary governed runs; never call a provider here."""

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        clock: Clock,
        ids: IdFactory,
        principal: Principal,
        *,
        provider: str,
        model: str,
        evidence: EmailSemanticEvaluationEvidence | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids
        self._principal = principal
        self._provider = provider
        self._model = model
        self._evidence = evidence
        self._governed = GovernedMemoryService(
            uow_factory,
            clock,
            ids,
            principal,
            policy_version=EMAIL_SEMANTIC_POLICY_VERSION,
        )

    @property
    def enabled(self) -> bool:
        return self._evidence is not None and (
            self._evidence.provider == self._provider
            and self._evidence.model == self._model
            and self._evidence.policy_version == EMAIL_SEMANTIC_POLICY_VERSION
            and self._evidence.implementation_sha256 == semantic_implementation_sha256()
        )

    async def register_source(
        self,
        source: EmailSemanticSource,
        *,
        run: Run | None = None,
        lease: WorkerLease | None = None,
    ) -> None:
        """Record immutable account-qualified provenance without duplicating the body."""
        async with self._uow_factory() as uow, uow.email.lock(self._principal):
            await self._validate_source(uow, source)
            await self._guard_run(uow, source, run, lease)
            await self._register(uow, source)

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
        if len(facts) > 20:
            raise ToolValidationError("semantic email proposal exceeds candidate limit")
        async with self._uow_factory() as uow, uow.email.lock(self._principal):
            await self._validate_source(uow, source)
            await self._guard_run(uow, source, run, lease)
            if run is not None:
                if run_id is not None and run_id != run.id:
                    raise ToolValidationError("semantic run identity differs from its audit")
                run_id = run.id if run.session_id == source.session_id else None
            link = await self._register(uow, source)
            for fact in facts:
                if (
                    fact.message_id != source.message_id
                    or fact.quote not in source.body
                    or fact.value not in fact.quote
                ):
                    raise ToolValidationError("semantic email fact lacks exact source grounding")
            formed: list[MemoryRecord] = []
            prior_facts = link.payload.get("facts", {})
            fact_ids = dict(prior_facts) if isinstance(prior_facts, dict) else {}
            for fact in facts:
                fact_key = hashlib.sha256(
                    json.dumps(
                        fact.model_dump(mode="json", exclude={"confidence"}),
                        sort_keys=True,
                    ).encode()
                ).hexdigest()
                if fact_key in fact_ids:
                    try:
                        prior = await uow.memories.get(
                            UUID(str(fact_ids[fact_key])), self._principal
                        )
                    except NotFoundError:
                        # An independently deleted/corrected belief must not be recreated.
                        continue
                    formed.append(prior)
                    continue
                statement = (
                    f"Email from {source.sender} on {source.sent_at.date().isoformat()} "
                    f"(mailbox {source.account_id}, thread {source.provider_thread_id}, "
                    f"message {source.message_id}) reports {fact.subject} "
                    f'{fact.predicate}: "{fact.value}".'
                )
                memory, _action = await self._governed._remember(
                    session_id=source.session_id,
                    run_id=run_id,
                    statement=statement,
                    subject=fact.subject,
                    scope=scope,
                    belief_type=fact.belief_type,
                    portability=Portability.CONTEXTUAL,
                    sensitivity=Sensitivity.SENSITIVE,
                    source_event_ids=[source.source_event_sequence],
                    origin_trust=TrustLevel.EXTERNAL_UNTRUSTED,
                    explicit=False,
                    authority=MemoryAuthority.INFERRED,
                    polarity=Polarity.ASSERT,
                    confidence=min(fact.confidence, 0.4),
                    valid_from=source.sent_at,
                    expires_at=source.sent_at + timedelta(days=30),
                    trigger=EMAIL_SEMANTIC_POLICY_VERSION,
                    record_audit=True,
                    derivation=MemoryDerivation.HYPOTHESIS,
                    longevity=MemoryLongevity.TENTATIVE,
                    evidence_at=source.sent_at,
                    attributed_external=True,
                    semantic_external=True,
                    existing_uow=uow,
                    audit_model=f"{self._provider}/{self._model}",
                )
                formed.append(memory)
                fact_ids[fact_key] = str(memory.id)
            memory_ids = sorted(
                {str(value) for value in _items(link.payload.get("memory_ids"))}.union(
                    str(item.id)
                    for item in formed
                    if item.authority is MemoryAuthority.INFERRED
                    and item.consolidation_policy_version == EMAIL_SEMANTIC_POLICY_VERSION
                )
            )
            if memory_ids != link.payload.get("memory_ids", []) or fact_ids != prior_facts:
                await uow.email.put(
                    link.model_copy(
                        update={
                            "revision": link.revision + 1,
                            "payload": {
                                **link.payload,
                                "memory_ids": memory_ids,
                                "facts": fact_ids,
                            },
                            "updated_at": self._clock.now(),
                        }
                    ),
                    expected_revision=link.revision,
                )
            return formed

    async def _guard_run(
        self,
        uow: RepositoryUnitOfWork,
        source: EmailSemanticSource,
        run: Run | None,
        lease: WorkerLease | None,
    ) -> None:
        if run is None:
            if lease is not None:
                raise ToolValidationError("semantic lease requires its owning run")
            return
        await uow.sessions.get(run.session_id, self._principal)
        if run.tenant_id != self._principal.tenant_id:
            raise ToolTrustRejectedError("semantic run belongs to another tenant")
        await uow.events.append(
            NewEvent(
                session_id=run.session_id,
                run_id=run.id,
                event_type="email.semantic.updated",
                actor_type="runtime",
                payload={
                    "source_key": semantic_source_key(
                        source.account_id, source.provider_thread_id, source.message_id
                    ),
                    "source_session_id": str(source.session_id),
                    "source_event_sequence": source.source_event_sequence,
                    "policy_version": EMAIL_SEMANTIC_POLICY_VERSION,
                },
            ),
            lease=lease,
        )

    async def _register(
        self,
        uow: RepositoryUnitOfWork,
        source: EmailSemanticSource,
    ) -> EmailRecord:
        key = semantic_source_key(source.account_id, source.provider_thread_id, source.message_id)
        current = await uow.email.get(self._principal, "semantic_source", key)
        if current is not None and current.payload.get("excluded"):
            raise ConflictError("this email source was excluded")
        fingerprint = hashlib.sha256(source.body.encode()).hexdigest()
        stored_passages = current.payload.get("passages", {}) if current else {}
        passages = dict(stored_passages) if isinstance(stored_passages, dict) else {}
        offset_key = str(source.body_offset)
        if offset_key in passages and passages[offset_key] != fingerprint:
            raise ToolTrustRejectedError("immutable email passage changed its body")
        passages[offset_key] = fingerprint
        occurrence = {
            "session_id": str(source.session_id),
            "event_sequence": source.source_event_sequence,
            "tool_name": source.tool_name,
            "body_offset": source.body_offset,
            "header_event_sequence": source.header_event_sequence,
            "header_session_id": str(source.header_session_id)
            if source.header_session_id
            else None,
        }
        occurrences = _items(current.payload.get("occurrences")) if current else []
        if current and occurrence in occurrences:
            return current
        occurrences.append(occurrence)
        now = self._clock.now()
        payload: dict[str, object] = {
            "account_id": source.account_id,
            "provider_thread_id": source.provider_thread_id,
            "message_id": source.message_id,
            "body_sha256": fingerprint,
            "passages": passages,
            "evidence_at": source.sent_at.isoformat(),
            "occurrences": occurrences,
            "memory_ids": current.payload.get("memory_ids", []) if current else [],
            "facts": current.payload.get("facts", {}) if current else {},
            "excluded": False,
        }
        record = EmailRecord(
            tenant_id=self._principal.tenant_id,
            principal_id=self._principal.principal_id,
            kind="semantic_source",
            key=key,
            revision=current.revision + 1 if current else 1,
            payload=payload,
            created_at=current.created_at if current else now,
            updated_at=now,
        )
        return await uow.email.put(record, expected_revision=current.revision if current else 0)

    async def _read_document(
        self,
        uow: RepositoryUnitOfWork,
        source: EmailSemanticSource,
        sequence: int,
        tool_name: str,
        session_id: UUID | None = None,
    ) -> tuple[dict[str, object], EventEnvelope]:
        events = await uow.events.list_after(
            session_id or source.session_id, sequence - 1, self._principal, limit=1
        )
        if not events or events[0].sequence != sequence:
            raise ToolTrustRejectedError("email source event is missing")
        event = events[0]
        if (
            event.event_type != "tool.call.completed"
            or event.actor_type != "runtime"
            or event.payload.get("name") != tool_name
        ):
            raise ToolTrustRejectedError("email source is not the completed first-party read")
        try:
            result = ToolResultItem.model_validate(event.payload.get("result_item"))
            text = "\n".join(part.text for part in result.content if isinstance(part, TextPart))
            document = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise ToolTrustRejectedError("email source result is invalid") from exc
        if (
            result.is_error
            or result.trust is not TrustLevel.EXTERNAL_UNTRUSTED
            or not isinstance(document, dict)
        ):
            raise ToolTrustRejectedError("email source result has invalid trust or identity")
        return document, event

    async def _validate_source(
        self,
        uow: RepositoryUnitOfWork,
        source: EmailSemanticSource,
    ) -> None:
        exclusion_key = hashlib.sha256(
            f"{source.account_id}:{source.provider_thread_id}".encode()
        ).hexdigest()
        if await uow.email.get(self._principal, "excluded_source", exclusion_key) is not None:
            raise ConflictError("this email thread was excluded")
        session = await uow.sessions.get(source.session_id, self._principal)
        bindings = session.metadata.get("email_account_servers", {})
        binding = bindings.get(source.account_id, {}) if isinstance(bindings, dict) else {}
        server = binding.get("read") if isinstance(binding, dict) else None
        if (
            not isinstance(server, str)
            or re.fullmatch(r"gmail(?:_[a-z][a-z0-9_]{0,31})?_read", server) is None
            or source.tool_name
            not in {f"mcp.{server}.get_thread_page", f"mcp.{server}.get_message_body"}
        ):
            raise ToolTrustRejectedError("email source has no immutable mailbox binding")
        body_document, event = await self._read_document(
            uow, source, source.source_event_sequence, source.tool_name
        )
        is_chunk = source.tool_name.endswith(".get_message_body")
        if is_chunk:
            if source.header_event_sequence is None:
                raise ToolTrustRejectedError("email passage lacks its original header event")
            if source.header_session_id is not None:
                header_session = await uow.sessions.get(source.header_session_id, self._principal)
                header_bindings = header_session.metadata.get("email_account_servers", {})
                header_binding = (
                    header_bindings.get(source.account_id, {})
                    if isinstance(header_bindings, dict)
                    else {}
                )
                if not isinstance(header_binding, dict) or header_binding.get("read") != server:
                    raise ToolTrustRejectedError("email header mailbox binding differs")
            document, header_event = await self._read_document(
                uow,
                source,
                source.header_event_sequence,
                f"mcp.{server}.get_thread_page",
                session_id=source.header_session_id,
            )
        else:
            document, header_event = body_document, event
        if document.get("thread_id") != source.provider_thread_id:
            raise ToolTrustRejectedError("email header thread identity differs")
        matches = [
            item
            for item in _items(document.get("messages"))
            if isinstance(item, dict) and item.get("id") == source.message_id
        ]
        if len(matches) != 1:
            raise ToolTrustRejectedError("email message is absent or ambiguous")
        message = matches[0]
        try:
            sent_at = datetime.fromtimestamp(int(message["internal_date"]) / 1000, tz=UTC)
        except (KeyError, ValueError, TypeError, OverflowError, OSError) as exc:
            raise ToolTrustRejectedError("email evidence date is invalid") from exc
        if (
            message.get("from") != source.sender
            or message.get("headers_complete") is not True
            or sent_at != source.sent_at
            or sent_at > header_event.created_at
        ):
            raise ToolTrustRejectedError("email header differs from the original normalized result")
        if is_chunk:
            if (
                body_document.get("message_id") != source.message_id
                or not message.get("history_id")
                or body_document.get("history_id") != message.get("history_id")
                or body_document.get("offset") != source.body_offset
                or body_document.get("body") != source.body
                or body_document.get("source_changed") is not False
                or body_document.get("body_available") is not True
                or (
                    body_document.get("complete") is not True
                    and body_document.get("next_offset")
                    != source.body_offset + len(source.body.encode())
                )
            ):
                raise ToolTrustRejectedError(
                    "email passage differs from its revision-bound tool result"
                )
        elif (
            source.body_offset != 0
            or message.get("body") != source.body
            or (
                message.get("body_complete") is not True
                and (
                    message.get("body_available") is not True
                    or message.get("next_body_offset") != len(source.body.encode())
                )
            )
        ):
            raise ToolTrustRejectedError(
                "email passage differs from the original normalized result"
            )

    async def exclude_source(self, account_id: str, thread_id: str, message_id: str) -> int:
        """Suppress reformation and forget linked inferred beliefs; retain owner corrections."""
        key = semantic_source_key(account_id, thread_id, message_id)
        async with self._uow_factory() as uow, uow.email.lock(self._principal):
            current = await uow.email.get(self._principal, "semantic_source", key)
            now = self._clock.now()
            if current is None:
                current = await uow.email.put(
                    EmailRecord(
                        tenant_id=self._principal.tenant_id,
                        principal_id=self._principal.principal_id,
                        kind="semantic_source",
                        key=key,
                        revision=1,
                        created_at=now,
                        updated_at=now,
                        payload={
                            "account_id": account_id,
                            "provider_thread_id": thread_id,
                            "message_id": message_id,
                            "excluded": True,
                            "memory_ids": [],
                        },
                    ),
                    expected_revision=0,
                )
            elif not current.payload.get("excluded"):
                current = await uow.email.put(
                    current.model_copy(
                        update={
                            "revision": current.revision + 1,
                            "payload": {**current.payload, "excluded": True},
                            "updated_at": now,
                        }
                    ),
                    expected_revision=current.revision,
                )
            removed = 0
            for value in _items(current.payload.get("memory_ids")):
                try:
                    memory = await uow.memories.get(UUID(str(value)), self._principal)
                except NotFoundError:
                    continue
                if memory.authority is not MemoryAuthority.INFERRED:
                    continue
                rejection = BeliefRejection(
                    id=self._ids.new_id(),
                    tenant_id=self._principal.tenant_id,
                    principal_id=self._principal.principal_id,
                    belief_id=memory.id,
                    kind=RejectionKind.DELETED,
                    subject=memory.subject,
                    statement=None,
                    statement_sha256=hashlib.sha256(
                        memory.statement.casefold().encode()
                    ).hexdigest(),
                    belief_type=memory.belief_type,
                    scope=memory.scope,
                    created_at=now,
                )
                await uow.memories.delete(memory.id, self._principal, rejection)
                removed += 1
            return removed


def semantic_implementation_sha256() -> str:
    """Bind evaluation to the extraction prompt, validator and memory lifecycle code."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for name in (
        "domain/email.py",
        "domain/email_semantics.py",
        "context/rendering.py",
        "memory/email_semantics.py",
        "memory/formation.py",
        "memory/retrieval.py",
        "memory/communication_sources.py",
        "runtime/email_tasks.py",
        "runtime/email_state.py",
        "application/email.py",
    ):
        digest.update(name.encode())
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def load_email_semantic_evidence(
    path: Path,
    *,
    provider: str,
    model: str,
    build_ref: str | None,
) -> EmailSemanticEvaluationEvidence:
    """Fail closed on malformed, stale-build, changed-code or wrong-model artifacts."""
    from agent_core.config import ConfigurationError

    try:
        evidence = EmailSemanticEvaluationEvidence.model_validate_json(path.read_text())
        if (
            evidence.provider != provider
            or evidence.model != model
            or build_ref is None
            or evidence.build_ref != build_ref
            or evidence.implementation_sha256 != semantic_implementation_sha256()
        ):
            raise ValueError("semantic evidence does not match the running configuration")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ConfigurationError("email semantic evaluation evidence did not pass") from exc
    return evidence
