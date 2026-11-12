"""Bounded research and immutable draft file values (ADR-0317)."""

from collections.abc import Iterable
from uuid import UUID

from pydantic import AwareDatetime, Field

from agent_core.domain.email_base import EmailValue
from agent_core.domain.errors import ConflictError

DRAFT_ATTACHMENT_BYTES = 15 * 1024 * 1024
DRAFT_ATTACHMENT_LIMIT = 10
DRAFT_ATTACHMENT_ORIGIN = "email_draft_attachment"
DRAFT_TARGET_KEY = "email_draft_thread"


def draft_transport_fits(body: str, sizes: Iterable[int]) -> bool:
    """Conservative MIME/outer-base64 admission under the existing 24 MiB cap.

    Reserve one MiB for bounded envelope, filenames and MIME headers. Text may
    use quoted-printable; attachments use wrapped base64, then Gmail wraps the
    complete MIME message in base64url. No file content is needed for admission.
    """
    text_bytes = 3 * len(body.encode("utf-8"))
    mime_bytes = 1024 * 1024 + text_bytes + 2 * ((text_bytes + 71) // 72)
    for size in sizes:
        encoded = 4 * ((size + 2) // 3)
        mime_bytes += encoded + 2 * ((encoded + 75) // 76)
    return 4 * ((mime_bytes + 2) // 3) <= 24 * 1024 * 1024


class EmailToolError(Exception):
    """A normalized unavailable/denied read; never contains provider content."""


class EmailModelResultError(EmailToolError):
    """A completed model response that is not a valid email result."""


class DraftPreparationError(ConflictError):
    """A content-free, owner-visible reason a researched proposal cannot be saved."""


class DraftAttachment(EmailValue):
    """Only verified source files can enter a draft's frozen manifest."""

    artifact_id: UUID
    account_id: str = Field(min_length=1, max_length=32)
    source_thread_id: str = Field(min_length=1, max_length=1024)
    message_id: str = Field(min_length=1, max_length=256)
    attachment_index: int = Field(ge=0, le=1000)
    filename: str = Field(min_length=1, max_length=1024)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(gt=0, le=DRAFT_ATTACHMENT_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MailResearchQuery(EmailValue):
    account_id: str = Field(min_length=1, max_length=32)
    query: str = Field(min_length=1, max_length=1024)
    reason: str = Field(min_length=1, max_length=500)


class CalendarResearchQuery(EmailValue):
    query: str = Field(min_length=1, max_length=500)
    time_min: AwareDatetime
    time_max: AwareDatetime


class DraftResearchPlan(EmailValue):
    """Read requests or a grounded proposed body; never outbound authority."""

    body: str = Field(default="", max_length=20000)
    mail_queries: list[MailResearchQuery] = Field(default_factory=list, max_length=3)
    calendar_queries: list[CalendarResearchQuery] = Field(default_factory=list, max_length=2)
    memory_queries: list[str] = Field(default_factory=list, max_length=2)
    attachment_ids: list[str] = Field(default_factory=list, max_length=DRAFT_ATTACHMENT_LIMIT)
    requires_attachments: bool = False
    preparation_issue: str | None = Field(default=None, max_length=500)
