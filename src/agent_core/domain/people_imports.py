"""Explicit bounded historical import requests and content-free progress."""

from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class ImportValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PeopleImportScope(ImportValue):
    session_ids: list[UUID] = Field(default_factory=list, max_length=100)
    account_ids: list[str] = Field(default_factory=list, max_length=10)
    email_source: Literal["retained", "mailbox"] = "retained"
    since: AwareDatetime
    until: AwareDatetime
    person_ids: list[UUID] = Field(default_factory=list, max_length=3)
    excluded_source_ids: list[UUID] = Field(default_factory=list, max_length=1000)
    max_records: int = Field(ge=1, le=10000)
    max_cost_usd: Decimal = Field(gt=0, le=100, allow_inf_nan=False)

    @model_validator(mode="after")
    def bounded_sources(self) -> "PeopleImportScope":
        if not self.session_ids and not self.account_ids:
            raise ValueError("an import requires explicitly selected sources")
        if self.email_source == "mailbox" and not self.account_ids:
            raise ValueError("mailbox discovery requires explicitly selected accounts")
        if self.since >= self.until:
            raise ValueError("import date range must be positive")
        for values in (
            self.session_ids,
            self.account_ids,
            self.person_ids,
            self.excluded_source_ids,
        ):
            if len(values) != len(set(values)):
                raise ValueError("import source selections must be unique")
        if any(not value or len(value) > 200 for value in self.account_ids):
            raise ValueError("invalid import account identifier")
        return self


class PeopleImportRequest(ImportValue):
    phase: Literal["preview", "apply", "resume"]
    session_id: UUID
    scope: PeopleImportScope
    operation_id: UUID | None = None
    expected_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def phase_target(self) -> "PeopleImportRequest":
        if self.phase in {"apply", "resume"} and (
            self.operation_id is None or self.expected_revision is None
        ):
            raise ValueError("starting an import requires its reviewed preview revision")
        if self.phase == "preview" and (
            self.operation_id is not None or self.expected_revision is not None
        ):
            raise ValueError("an import preview cannot target an existing operation")
        return self


class PeopleImportCancel(ImportValue):
    expected_revision: int = Field(ge=1)


class PeopleImportRetry(ImportValue):
    """An exact failed Chat source; its text remains only in the event store."""

    session_id: UUID
    event_id: int = Field(ge=1)
    sequence: int = Field(ge=1)
    created_at: AwareDatetime


class PeopleMailboxMessage(ImportValue):
    message_id: str = Field(min_length=1, max_length=1024)
    header_session_id: UUID
    header_sequence: int = Field(ge=1)
    offset: int = Field(ge=0)
    passages: int = Field(default=1, ge=1, le=16)


class PeopleMailboxProgress(ImportValue):
    account_index: int = Field(default=0, ge=0, le=10)
    search_page: str | None = Field(default=None, max_length=4096)
    thread_ids: list[str] = Field(default_factory=list, max_length=25)
    thread_index: int = Field(default=0, ge=0, le=25)
    thread_page: str | None = Field(default=None, max_length=4096)
    pending: PeopleMailboxMessage | None = None
    records_read: int = Field(default=0, ge=0, le=10000)
    read_calls: int = Field(default=0, ge=0, le=200020)
    complete: bool = False
    partial: bool = False


ImportState = Literal[
    "preview", "queued", "running", "budget_paused", "completed", "cancelled", "failed"
]


class PeopleImportView(ImportValue):
    id: UUID
    audit_session_id: UUID
    revision: int
    state: ImportState
    scope: PeopleImportScope
    records_read: int
    mailbox_records_read: int = 0
    mailbox_read_complete: bool = False
    records_processed: int
    records_excluded: int
    failures: int
    spent_usd: Decimal
    reserved_usd: Decimal
    source_read_complete: bool
    analysis_complete: bool
    known_records: int | None
    remaining_records: int | None
    coverage: str
    error_code: str | None
    run_id: UUID | None
