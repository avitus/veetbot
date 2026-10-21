"""What a belief's cited sources may show the owner (ADR-0224 part B).

The route follows the People evidence pattern, not ADR-0189 source recovery:
it reads only the event log, never a mailbox, and shows text only when the
text is the owner's own authenticated words, the caller may already read the
conversation that holds them, and the request asked for the restricted
ceiling. Every other source is navigation identifiers; a source that is gone
is its reference and a reason. Nothing here is written to a trace, index,
cache or model context.
"""

from __future__ import annotations

from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import EventEnvelope, is_schedule_instruction_event
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.memory import MemoryRecord, Sensitivity
from agent_core.domain.people_sources import source_id
from agent_core.domain.policies import TrustLevel
from agent_core.domain.views import (
    MEMORY_EVIDENCE_LIMIT,
    MEMORY_EXCERPT_MAX_CHARS,
    MemoryEvidenceItemView,
    MemoryEvidenceKind,
    MemoryEvidenceView,
)
from agent_core.ports.persistence import RepositoryUnitOfWork

# The owner speaking: a chat message, an assertion on a People page, or a
# wording correction from the memory browser (ADR-0224 part A).
OWNER_TEXT_EVENTS = {
    "user.message.created": "content",
    "people.owner_assertion": "content",
    "memory.owner_correction": "statement",
}


async def memory_evidence(
    uow: RepositoryUnitOfWork, principal: Principal, record: MemoryRecord, ceiling: Sensitivity
) -> MemoryEvidenceView:
    """At most five cited sources, newest first, each shown no further than it may be."""
    sequences = sorted(set(record.source_event_ids), reverse=True)[:MEMORY_EVIDENCE_LIMIT]
    session_id = record.source_session_id
    try:
        await uow.sessions.get(session_id, principal)
    except NotFoundError:
        return MemoryEvidenceView(items=[_erased(session_id, sequence) for sequence in sequences])
    items = []
    for sequence in sequences:
        events = await uow.events.list_after(session_id, sequence - 1, principal, limit=1)
        event = next((row for row in events if row.sequence == sequence), None)
        if event is None or await uow.people.source_suppressed(
            principal, source_id(principal, session_id, sequence)
        ):
            items.append(_erased(session_id, sequence))
            continue
        items.append(_shown(event, principal, ceiling))
    return MemoryEvidenceView(items=items)


def _erased(session_id: UUID, sequence: int) -> MemoryEvidenceItemView:
    return MemoryEvidenceItemView(
        source_kind="unknown",
        session_id=session_id,
        event_sequence=sequence,
        evidence_at=None,
        withheld="erased",
    )


def _shown(
    event: EventEnvelope, principal: Principal, ceiling: Sensitivity
) -> MemoryEvidenceItemView:
    kind = source_kind(event, principal)
    item = MemoryEvidenceItemView(
        source_kind=kind,
        session_id=event.session_id,
        event_sequence=event.sequence,
        evidence_at=event.created_at,
        withheld="not_owner_text",
    )
    if kind != "owner":
        return item
    if "session.read" not in principal.scopes:
        return item.model_copy(update={"withheld": "scope"})
    if ceiling is not Sensitivity.RESTRICTED:
        return item.model_copy(update={"withheld": "ceiling"})
    text = owner_text(event)
    if text is None or contains_secret_material(text) or contains_injection_pattern(text):
        return item.model_copy(update={"withheld": "hazard"})
    return item.model_copy(update={"excerpt": excerpt(text), "withheld": None})


def source_kind(event: EventEnvelope, principal: Principal) -> MemoryEvidenceKind:
    """The closed source vocabulary, from the event alone."""
    if event.event_type == "user.message.created":
        if is_schedule_instruction_event(event):
            return "scheduler"
        if event.payload.get("trust") == TrustLevel.EXTERNAL_UNTRUSTED.value:
            # A message the owner's iPhone relayed: third-party text.
            return "sms"
    if event.event_type in OWNER_TEXT_EVENTS:
        if event.actor_type in {"user", "principal"} and event.actor_id == principal.principal_id:
            return "owner"
        return "unknown"
    if event.event_type.startswith("email."):
        return "email"
    if event.event_type.startswith(("call.", "bland.")):
        return "call"
    if event.event_type.startswith(("sms.", "device.")):
        return "sms"
    if event.event_type.startswith("schedule."):
        return "scheduler"
    if event.event_type.startswith("tool."):
        name = str(event.payload.get("name", ""))
        if name.startswith("email.") or "gmail" in name:
            return "email"
        if name.startswith("call.") or "bland" in name:
            return "call"
        if name.startswith(("sms.", "device.")):
            return "sms"
        return "tool"
    return "unknown"


def owner_text(event: EventEnvelope) -> str | None:
    """The text of an owner event: a string, or the text parts of a message."""
    value = event.payload.get(OWNER_TEXT_EVENTS[event.event_type])
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        texts = [
            part["text"]
            for part in value
            if isinstance(part, dict)
            and part.get("kind") == "text"
            and isinstance(part.get("text"), str)
        ]
        return " ".join(texts) if texts else None
    return None


def excerpt(text: str) -> str:
    """The start of the text, cut at a word boundary within the display limit."""
    clean = " ".join(text.split())
    if len(clean) <= MEMORY_EXCERPT_MAX_CHARS:
        return clean
    cut = clean[: MEMORY_EXCERPT_MAX_CHARS - 1]
    boundary = cut.rfind(" ")
    if boundary > MEMORY_EXCERPT_MAX_CHARS // 2:
        cut = cut[:boundary]
    return cut.rstrip() + "…"
