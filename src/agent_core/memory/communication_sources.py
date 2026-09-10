"""Typed, attributed communication sources for automatic memory formation."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from agent_core.domain.agents import Principal
from agent_core.domain.events import EventEnvelope
from agent_core.domain.memory import (
    MEMORY_SUBJECT_MAX_LENGTH,
    BeliefType,
    EvidenceSpan,
    MemoryCandidate,
    MemoryClaimKind,
    MemoryDerivation,
    MemoryExtractionResult,
    MemoryLongevity,
    Portability,
    Sensitivity,
)
from agent_core.domain.messages import TextPart, ToolResultItem
from agent_core.domain.policies import TrustLevel
from agent_core.ports.memory import MemoryCandidateExtractor

COMMUNICATION_ATTRIBUTION_VERSION = "communication-attribution-v1"
MAX_COMMUNICATION_CANDIDATES = 20
_GMAIL_TOOL = re.compile(
    r"^mcp\.gmail(?P<account>_[a-z][a-z0-9_]{0,31})?_read\."
    r"(?P<operation>search_threads|get_thread)$"
)
_SMS_PREFIX = "An SMS arrived on your paired device from "
_SMS_SENDER_END = ". Triage it under your standing instructions;"
_SMS_BODY_MARKER = "\n\nMessage: "


class FormationSourceKind(StrEnum):
    """The authority class of one event admitted by automatic formation."""

    OWNER_ASSERTION = "owner_assertion"
    ATTRIBUTED_COMMUNICATION = "attributed_communication"


@dataclass(frozen=True, slots=True)
class FormationSource:
    """The bounded formation view of one source event."""

    event: EventEnvelope
    kind: FormationSourceKind
    channel: str
    text: str


@dataclass(frozen=True, slots=True)
class _CommunicationItem:
    key: str
    source: FormationSource
    correspondent: str
    subject: str
    excerpt: str
    detail_rank: int


def _content_text(event: EventEnvelope) -> str:
    content = event.payload.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    texts: list[str] = []
    for raw in content:
        if not isinstance(raw, dict):
            continue
        try:
            part = TextPart.model_validate(raw)
        except ValueError:
            continue
        texts.append(part.text)
    return "\n".join(texts).strip()


def _tool_result_text(event: EventEnvelope) -> str | None:
    raw = event.payload.get("result_item")
    if not isinstance(raw, dict):
        return None
    try:
        result = ToolResultItem.model_validate(raw)
    except ValueError:
        return None
    if result.is_error or result.trust is not TrustLevel.EXTERNAL_UNTRUSTED:
        return None
    texts = [part.text for part in result.content if isinstance(part, TextPart)]
    rendered = "\n".join(texts).strip()
    return rendered or None


def _gmail_document(event: EventEnvelope) -> tuple[str, str, str, dict[str, object]] | None:
    if event.event_type != "tool.call.completed" or event.actor_type != "tool":
        return None
    name = event.payload.get("name")
    if not isinstance(name, str) or (match := _GMAIL_TOOL.fullmatch(name)) is None:
        return None
    text = _tool_result_text(event)
    if text is None:
        return None
    try:
        document = json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict):
        return None
    account_group = match.group("account")
    account = "default" if account_group is None else account_group.removeprefix("_")
    operation = match.group("operation")
    collection_name = "messages" if operation == "get_thread" else "threads"
    if not isinstance(document.get(collection_name), list):
        return None
    return account, operation, text, document


def _sms_parts(event: EventEnvelope, principal: Principal) -> tuple[str, str] | None:
    origin = event.payload.get("origin")
    content = _content_text(event)
    if (
        event.event_type != "user.message.created"
        or event.actor_type != "device"
        or event.actor_id != principal.principal_id
        or event.payload.get("trust") != TrustLevel.EXTERNAL_UNTRUSTED.value
        or not isinstance(origin, dict)
        or origin.get("kind") != "device_ingest"
        or origin.get("channel") != "sms"
        or not content.startswith(_SMS_PREFIX)
        or _SMS_SENDER_END not in content
        or _SMS_BODY_MARKER not in content
    ):
        return None
    sender_end = content.find(_SMS_SENDER_END, len(_SMS_PREFIX))
    marker = content.find(_SMS_BODY_MARKER, sender_end)
    if sender_end < 0 or marker < 0:
        return None
    sender = content[len(_SMS_PREFIX) : sender_end].strip()
    body = content[marker + len(_SMS_BODY_MARKER) :].strip()
    if not sender or not body:
        return None
    return sender, body


def formation_source(event: EventEnvelope, principal: Principal) -> FormationSource | None:
    """Classify one event without upgrading its context or policy trust."""

    if (
        event.event_type == "user.message.created"
        and event.actor_type == "principal"
        and event.actor_id == principal.principal_id
        and (text := _content_text(event))
    ):
        return FormationSource(
            event=event,
            kind=FormationSourceKind.OWNER_ASSERTION,
            channel="direct",
            text=text,
        )
    origin = event.payload.get("origin")
    if (
        event.event_type == "user.message.created"
        and event.actor_type == "surface"
        and event.actor_id == principal.principal_id
        and event.payload.get("trust") != TrustLevel.EXTERNAL_UNTRUSTED.value
        and isinstance(origin, dict)
        and origin.get("kind") == "surface"
        and isinstance(origin.get("surface_id"), str)
        and isinstance(origin.get("external_update_id"), str | int)
        and (text := _content_text(event))
    ):
        channel = origin.get("platform")
        return FormationSource(
            event=event,
            kind=FormationSourceKind.OWNER_ASSERTION,
            channel=channel if isinstance(channel, str) and channel else "surface",
            text=text,
        )
    if (sms := _sms_parts(event, principal)) is not None:
        _sender, body = sms
        return FormationSource(
            event=event,
            kind=FormationSourceKind.ATTRIBUTED_COMMUNICATION,
            channel="sms",
            text=body,
        )
    if (gmail := _gmail_document(event)) is not None:
        account, _operation, text, _document = gmail
        return FormationSource(
            event=event,
            kind=FormationSourceKind.ATTRIBUTED_COMMUNICATION,
            channel=f"gmail:{account}",
            text=text,
        )
    return None


def formation_sources(
    events: Sequence[EventEnvelope], principal: Principal
) -> dict[int, FormationSource]:
    """Return every admitted source in event order, keyed by sequence."""

    return {
        source.event.sequence: source
        for event in sorted(events, key=lambda item: item.sequence)
        if (source := formation_source(event, principal)) is not None
    }


def _compact(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", " ").split())[:maximum].strip()


def _source_span(raw: str, *values: str) -> str:
    for value in values:
        if not value:
            continue
        encoded = json.dumps(value, ensure_ascii=False)[1:-1]
        if encoded and encoded in raw:
            return encoded[:8192]
    return raw[:8192]


def _gmail_items(event: EventEnvelope, principal: Principal) -> list[_CommunicationItem]:
    parsed = _gmail_document(event)
    source = formation_source(event, principal)
    if parsed is None or source is None:
        return []
    account, operation, _raw, document = parsed
    items: list[_CommunicationItem] = []
    if operation == "search_threads":
        collection = document.get("threads")
        assert isinstance(collection, list)
        for entry in collection:
            if not isinstance(entry, dict):
                continue
            thread_id = _compact(entry.get("thread_id"), maximum=1024)
            senders = entry.get("senders")
            correspondent = ""
            if isinstance(senders, list):
                correspondent = _compact(next(iter(senders), ""), maximum=200)
            subject = _compact(entry.get("subject"), maximum=240)
            excerpt = _compact(entry.get("snippet"), maximum=600)
            if not thread_id or not any((correspondent, subject, excerpt)):
                continue
            items.append(
                _CommunicationItem(
                    key=f"gmail:{account}:{thread_id}",
                    source=source,
                    correspondent=correspondent,
                    subject=subject,
                    excerpt=excerpt,
                    detail_rank=0,
                )
            )
        return items
    thread_id = _compact(document.get("thread_id"), maximum=1024)
    messages = document.get("messages")
    assert isinstance(messages, list)
    message = next((item for item in reversed(messages) if isinstance(item, dict)), None)
    if not thread_id or message is None:
        return []
    correspondent = _compact(message.get("from"), maximum=200)
    subject = _compact(message.get("subject"), maximum=240)
    excerpt = _compact(message.get("body"), maximum=600)
    if not any((correspondent, subject, excerpt)):
        return []
    return [
        _CommunicationItem(
            key=f"gmail:{account}:{thread_id}",
            source=source,
            correspondent=correspondent,
            subject=subject,
            excerpt=excerpt,
            detail_rank=1,
        )
    ]


def _sms_item(event: EventEnvelope, principal: Principal) -> _CommunicationItem | None:
    parts = _sms_parts(event, principal)
    source = formation_source(event, principal)
    origin = event.payload.get("origin")
    if parts is None or source is None or not isinstance(origin, dict):
        return None
    sender, body = parts
    digest = origin.get("digest")
    key_suffix = (
        digest
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
        else hashlib.sha256(f"{sender}\n{body}".encode()).hexdigest()
    )
    return _CommunicationItem(
        key=f"sms:{key_suffix}",
        source=source,
        correspondent=_compact(sender, maximum=200),
        subject="",
        excerpt=_compact(body, maximum=600),
        detail_rank=1,
    )


def _candidate(item: _CommunicationItem, scope: str) -> MemoryCandidate:
    if item.source.channel.startswith("gmail:"):
        account = item.source.channel.split(":", 1)[1]
        statement = f"A Gmail message in the user's {account} mailbox"
        raw = item.source.text
    else:
        statement = "An SMS to the user"
        raw = item.source.text
    if item.correspondent:
        statement += f" from {item.correspondent}"
    if item.subject:
        statement += f" had subject {json.dumps(item.subject, ensure_ascii=False)}"
    if item.excerpt and item.source.channel != "sms":
        conjunction = " and said " if item.subject else " said "
        statement += conjunction + json.dumps(item.excerpt, ensure_ascii=False)
    elif item.source.channel == "sms":
        statement += " was received"
    statement += "."
    subject = item.key
    if len(subject) > MEMORY_SUBJECT_MAX_LENGTH:
        prefix = subject.split(":", 2)[:2]
        subject = ":".join((*prefix, hashlib.sha256(subject.encode()).hexdigest()))
    return MemoryCandidate(
        belief_type=BeliefType.FACT,
        subject=subject,
        statement=statement[:8192],
        source_event_ids=[item.source.event.sequence],
        model_confidence=0.4,
        proposed_scope=scope,
        proposed_portability=Portability.LOCAL,
        sensitivity_guess=Sensitivity.SENSITIVE,
        claim_kind=MemoryClaimKind.PROJECT_FACT,
        derivation=MemoryDerivation.HYPOTHESIS,
        longevity=MemoryLongevity.TENTATIVE,
        evidence_spans=[
            EvidenceSpan(
                source_event_id=item.source.event.sequence,
                text=(
                    item.excerpt[:8192]
                    if item.source.channel == "sms"
                    else _source_span(raw, item.excerpt, item.subject, item.correspondent)
                ),
            )
        ],
    )


def communication_candidates(
    events: Sequence[EventEnvelope], *, principal: Principal, scope: str
) -> list[MemoryCandidate]:
    """Render bounded, attributed candidates from recognized communications."""

    selected: dict[str, _CommunicationItem] = {}
    for event in sorted(events, key=lambda item: item.sequence):
        items = _gmail_items(event, principal)
        if (sms := _sms_item(event, principal)) is not None:
            items.append(sms)
        for item in items:
            current = selected.get(item.key)
            if current is None or (item.detail_rank, item.source.event.sequence) >= (
                current.detail_rank,
                current.source.event.sequence,
            ):
                selected[item.key] = item
    ordered = sorted(selected.values(), key=lambda item: item.source.event.sequence)
    return [_candidate(item, scope) for item in ordered[:MAX_COMMUNICATION_CANDIDATES]]


def communication_candidate_fingerprints(
    events: Sequence[EventEnvelope], *, principal: Principal, scope: str
) -> frozenset[str]:
    """Exact locally-rendered candidates the service may admit as correspondence."""

    return frozenset(
        candidate.model_dump_json()
        for candidate in communication_candidates(events, principal=principal, scope=scope)
    )


class AttributedCommunicationCandidateExtractor:
    """Compose conservative communication memories outside evaluated model policies."""

    def __init__(self, delegate: MemoryCandidateExtractor) -> None:
        self._delegate = delegate
        self.name = f"{delegate.name}+{COMMUNICATION_ATTRIBUTION_VERSION}"

    @property
    def last_audit(self) -> object | None:
        return getattr(self._delegate, "last_audit", None)

    async def extract(
        self,
        events: list[EventEnvelope],
        *,
        principal: Principal,
        scope: str,
    ) -> list[MemoryCandidate] | MemoryExtractionResult:
        # Paired surface messages are owner assertions. Existing extractors are
        # intentionally frozen around the principal actor shape, so the adapter
        # presents an in-memory projection with the same source sequence rather
        # than changing their evaluated prompts and schemas.
        projected: list[EventEnvelope] = []
        sources = formation_sources(events, principal)
        for event in events:
            source = sources.get(event.sequence)
            if (
                source is not None
                and source.kind is FormationSourceKind.OWNER_ASSERTION
                and event.actor_type == "surface"
            ):
                projected.append(
                    event.model_copy(
                        update={"actor_type": "principal", "actor_id": principal.principal_id}
                    )
                )
            else:
                projected.append(event)
        delegated = await self._delegate.extract(projected, principal=principal, scope=scope)
        combined = [
            *delegated,
            *communication_candidates(events, principal=principal, scope=scope),
        ]
        if isinstance(delegated, MemoryExtractionResult):
            return MemoryExtractionResult(
                combined,
                provider_failure=delegated.provider_failure,
            )
        return combined
