"""Provider-neutral, owner-scoped email experience values (Milestone 26)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from email.utils import getaddresses
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.email_semantics import EmailSemanticFact

EMAIL_POLICY_VERSION = "email-experience@1"
EMAIL_DAILY_CEILING = Decimal("20")
EMAIL_MONTHLY_CEILING = Decimal("200")
EMAIL_SLICE_RESERVATION = Decimal("1")


class EmailValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EmailRecord(EmailValue):
    """One independently revisioned projection; payloads contain typed values below."""

    tenant_id: str
    principal_id: str
    kind: str
    key: str
    revision: int = Field(ge=1)
    payload: dict[str, object]
    created_at: datetime
    updated_at: datetime


class EmailAccount(EmailValue):
    id: str
    label: str
    email_address: str | None = None
    verified_addresses: list[str] = Field(default_factory=list)
    status: Literal["ready", "unavailable", "syncing"] = "unavailable"
    last_synced_at: datetime | None = None
    history_complete: bool = False
    history_processed: int = 0
    inbox_complete: bool = False
    history_id: str | None = None
    inbox_cursor: str | None = None
    history_cursor: str | None = None
    history_window: int = 0
    error: str | None = None


class EmailAttachment(EmailValue):
    filename: str
    mime_type: str
    size: int | None = None


class EmailMessage(EmailValue):
    id: str
    sender: str
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str = ""
    body: str = ""
    body_offset: int = Field(default=0, ge=0)
    sent_at: datetime
    complete: bool = False
    attachments: list[EmailAttachment] = Field(default_factory=list)
    reply_to: list[str] = Field(default_factory=list)
    message_id_header: str | None = None
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    direction: Literal["sent", "received"] = "received"


class EmailThread(EmailValue):
    id: UUID
    account_id: str
    provider_thread_id: str
    subject: str
    senders: list[str] = Field(default_factory=list)
    updated_at: datetime
    revision: int = 1
    summary: str = "Assessment pending"
    reason: str = "Assessment pending"
    needs_reply: bool = False
    draft_id: UUID | None = None
    session_id: UUID | None = None
    priority: float = 0
    complete: bool = False
    messages: list[EmailMessage] = Field(default_factory=list)
    source_fingerprint: str = ""
    profile_revision: int = 0
    assessment_version: str = ""
    topics: list[str] = Field(default_factory=list)
    dismissed_revision: int | None = None
    last_accessed_at: datetime
    in_inbox: bool = True
    source_session_ids: list[UUID] = Field(default_factory=list)
    reply_blocked_reason: str | None = None


class EmailDraftStatus(StrEnum):
    GENERATING = "generating"
    READY = "ready"
    AWAITING_APPROVAL = "awaiting_approval"
    SENDING = "sending"
    SENT = "sent"
    UNCERTAIN = "uncertain"
    FAILED = "failed"
    DISCARDED = "discarded"


class EmailDraft(EmailValue):
    id: UUID
    thread_id: UUID
    account_id: str
    revision: int = 1
    source_revision: int
    profile_revision: int = 0
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    bcc: list[str] = Field(default_factory=list)
    subject: str
    body: str
    status: EmailDraftStatus = EmailDraftStatus.READY
    stale: bool = False
    run_id: UUID | None = None
    approval_id: UUID | None = None
    session_id: UUID | None = None
    updated_at: datetime
    in_reply_to: str | None = None
    references: list[str] = Field(default_factory=list)
    owner_edited: bool = False
    generated_body_digest: str | None = None
    send_claim_id: UUID | None = None
    provider_thread_id: str | None = None
    send_tool_name: str | None = None


class EmailFeedback(EmailValue):
    id: UUID
    thread_id: UUID
    target: Literal["thread", "person", "topic"]
    judgment: Literal["important", "less_important", "needs_reply", "no_reply_needed"]
    target_values: list[str]
    explanation: str | None = None
    created_at: datetime
    undone_at: datetime | None = None


class EmailOperation(EmailValue):
    operation_id: UUID
    run_id: UUID
    status: str
    replayed: bool = False


class EmailTask(EmailValue):
    """Typed server-owned intent; never synthesized owner conversation text."""

    id: UUID
    run_id: UUID
    session_id: UUID
    kind: Literal["refresh", "draft", "send"]
    account_ids: list[str]
    thread_id: UUID | None = None
    draft_id: UUID | None = None
    expected_revision: int | None = None
    instruction: str | None = None
    created_at: datetime
    reservation: Decimal = Decimal("0")
    settled_cost: Decimal | None = None
    stage: str = "queued"
    state: dict[str, object] = Field(default_factory=dict)


class EmailDraftEdit(EmailValue):
    expected_revision: int = Field(ge=1)
    source_revision: int | None = Field(default=None, ge=1)
    to: list[str] = Field(min_length=1, max_length=100)
    cc: list[str] = Field(default_factory=list, max_length=100)
    bcc: list[str] = Field(default_factory=list, max_length=100)
    subject: str = Field(max_length=998)
    body: str = Field(max_length=500_000)


class EmailLearningState(EmailValue):
    paused: bool = False
    profile_revision: int = 1
    excluded_sources: int = 0
    style_examples: int = 0
    history_processed: int = 0
    history_complete: bool = False


class EmailAssessment(EmailValue):
    summary: str = Field(max_length=1000)
    reason: str = Field(max_length=1000)
    topics: list[str] = Field(max_length=12)
    content_importance: float = Field(ge=0, le=1)
    relationship_importance: float = Field(ge=0, le=1)
    urgency: float = Field(ge=0, le=1)
    needs_reply: bool
    bulk: bool
    reply_blocked_reason: str | None = Field(default=None, max_length=1000)
    supported_evidence: list[str] = Field(default_factory=list, max_length=10)
    relationship_memory_ids: list[str] = Field(default_factory=list, max_length=20)
    semantic_facts: list[EmailSemanticFact] = Field(default_factory=list, max_length=20)


class EmailDraftBody(EmailValue):
    body: str = Field(min_length=1, max_length=20000)


class EmailSyncState(EmailValue):
    """Cursors advance only after the corresponding queued projections commit."""

    model_config = ConfigDict(extra="forbid", frozen=False)

    inbox_pending: list[str] = Field(default_factory=list)
    inbox_next: str | None = None
    inbox_page_open: bool = False
    change_pending: list[str] = Field(default_factory=list)
    change_events: dict[str, dict[str, str]] = Field(default_factory=dict)
    change_sequence: int = 0
    change_start: str | None = None
    change_next: str | None = None
    change_history: str | None = None
    change_cursor: str | None = None
    change_page_open: bool = False
    history_pending: list[str] = Field(default_factory=list)
    history_next: str | None = None
    history_page_open: bool = False
    anchor: str | None = None


def addresses(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if any(character in value for character in "\r\n\x00"):
            raise ValueError("mail address contains a header delimiter")
        parsed = getaddresses([value])
        if len(parsed) != 1 or "@" not in parsed[0][1] or any(c.isspace() for c in parsed[0][1]):
            raise ValueError("mail address is malformed")
        normalized = parsed[0][1].casefold()
        if normalized not in result:
            result.append(normalized)
    return result


def feedback_matches(feedback: EmailFeedback, thread: EmailThread) -> bool:
    if feedback.undone_at is not None:
        return False
    if feedback.target == "thread":
        return feedback.thread_id == thread.id
    candidates = addresses(thread.senders) if feedback.target == "person" else thread.topics
    return bool(set(feedback.target_values) & set(candidates))


def apply_feedback(thread: EmailThread, feedback: list[EmailFeedback]) -> EmailThread:
    """Rebuild from source assessment plus surviving explicit owner evidence."""
    selected = thread
    for item in sorted(feedback, key=lambda value: (value.created_at, str(value.id))):
        if not feedback_matches(item, thread):
            continue
        updates: dict[str, object] = {
            "reason": item.explanation or f"Your {item.target} preference"
        }
        if item.judgment in {"important", "less_important"}:
            updates["priority"] = 1.0 if item.judgment == "important" else 0.0
        else:
            updates["needs_reply"] = item.judgment == "needs_reply"
        selected = selected.model_copy(update=updates)
    return selected
