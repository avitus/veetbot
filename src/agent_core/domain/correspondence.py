"""Generated correspondence summaries and the retained original email (ADR-0126)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from agent_core.domain.email_base import EmailValue
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material

# Each Email refresh summarizes at most this many exchanges.
CORRESPONDENCE_SUMMARIES_PER_SLICE = 4
# No summary call starts with less of the slice deadline left than this.
CORRESPONDENCE_SUMMARY_DEADLINE_SECONDS = 30
# The longest original text one People history read returns.
RETAINED_TEXT_WINDOW = 8000


class EmailCorrespondenceSummary(EmailValue):
    """The model's gist of one message and the exact words it rests on."""

    summary: str = Field(
        min_length=1,
        max_length=240,
        description="One or two plain sentences on what this message says or asks.",
    )
    evidence: str = Field(
        min_length=1,
        max_length=300,
        description="Words copied exactly from the message body or subject.",
    )


class CorrespondenceSummaryWork(EmailValue):
    """One exchange awaiting its summary, with the verified passage to summarize."""

    interaction_id: UUID
    source_id: UUID
    account_id: str = Field(min_length=1)
    provider_thread_id: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    direction: Literal["incoming", "outgoing"]
    sender: str
    to: str | None = None
    cc: str | None = None
    subject: str = ""
    sent_at: AwareDatetime
    passage: str = Field(min_length=1)
    passage_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    retrying: bool = False


class RetainedEmailMessage(EmailValue):
    """One bounded window of a message's retained original text."""

    account_id: str
    provider_thread_id: str
    message_id: str
    sender: str
    to: str | None = None
    cc: str | None = None
    subject: str = ""
    sent_at: AwareDatetime
    text: str = Field(max_length=RETAINED_TEXT_WINDOW)
    offset: int = Field(ge=0)
    next_offset: int | None = Field(default=None, ge=1)
    # The retained text reaches the end of the message.
    complete: bool


def header_text(value: object, *, limit: int = 2000) -> str | None:
    """A header value as bounded text; list headers are joined."""
    if isinstance(value, list):
        value = ", ".join(item for item in value if isinstance(item, str))
    if not isinstance(value, str) or not value.strip():
        return None
    return value[:limit]


def summary_text(result: EmailCorrespondenceSummary | None, work: CorrespondenceSummaryWork) -> str:
    """The storable gist, or an empty string when the result must not be kept."""
    if result is None:
        return ""
    text = " ".join(result.summary.split())
    grounded = result.evidence in work.passage or (
        bool(work.subject) and result.evidence in work.subject
    )
    if (
        not text
        or not grounded
        or contains_injection_pattern(text)
        or contains_secret_material(text)
    ):
        return ""
    return text
