"""Scheduled-run domain values."""

from __future__ import annotations

from calendar import monthrange
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_core.domain.agents import Principal
from agent_core.domain.runs import RunLimits


class CadenceKind(StrEnum):
    ONCE = "ONCE"
    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    MONTHLY = "MONTHLY"
    YEARLY = "YEARLY"
    INTERVAL = "INTERVAL"


class OnceCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[CadenceKind.ONCE] = CadenceKind.ONCE
    at: datetime

    @field_validator("at")
    @classmethod
    def at_is_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("one-time cadence requires an aware instant")
        return value.astimezone(UTC)


class DailyCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[CadenceKind.DAILY] = CadenceKind.DAILY
    local_time: time
    timezone: str

    @field_validator("local_time")
    @classmethod
    def local_time_is_civil_second(cls, value: time) -> time:
        return _validate_local_time(value)

    @field_validator("timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        return _validate_timezone(value)


class WeeklyCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[CadenceKind.WEEKLY] = CadenceKind.WEEKLY
    local_time: time
    weekdays: tuple[int, ...]
    timezone: str

    @field_validator("local_time")
    @classmethod
    def local_time_is_civil_second(cls, value: time) -> time:
        return _validate_local_time(value)

    @field_validator("weekdays")
    @classmethod
    def weekdays_are_unique_iso_values(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if not value or any(day < 1 or day > 7 for day in value):
            raise ValueError("weekdays must contain values 1 through 7")
        if len(set(value)) != len(value):
            raise ValueError("weekdays must be unique")
        return tuple(sorted(value))

    @field_validator("timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        return _validate_timezone(value)


class MonthlyCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[CadenceKind.MONTHLY] = CadenceKind.MONTHLY
    local_time: time
    days_of_month: tuple[int, ...] = ()
    last_day: bool = False
    timezone: str

    @field_validator("local_time")
    @classmethod
    def local_time_is_civil_second(cls, value: time) -> time:
        return _validate_local_time(value)

    @field_validator("days_of_month")
    @classmethod
    def days_are_unique_calendar_values(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(day < 1 or day > 31 for day in value):
            raise ValueError("days_of_month must contain values 1 through 31")
        if len(set(value)) != len(value):
            raise ValueError("days_of_month must be unique")
        return tuple(sorted(value))

    @field_validator("timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        return _validate_timezone(value)

    @model_validator(mode="after")
    def selector_is_present(self) -> MonthlyCadence:
        if not self.days_of_month and not self.last_day:
            raise ValueError("monthly cadence requires a numbered-day or last-day selector")
        return self


class MonthDay(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    month: int = Field(ge=1, le=12)
    day: int = Field(ge=1, le=31)

    @model_validator(mode="after")
    def date_is_possible(self) -> MonthDay:
        if self.day > monthrange(2000, self.month)[1]:
            raise ValueError("yearly date must be a possible Gregorian date")
        return self


class YearlyCadence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[CadenceKind.YEARLY] = CadenceKind.YEARLY
    local_time: time
    dates: tuple[MonthDay, ...]
    timezone: str

    @field_validator("local_time")
    @classmethod
    def local_time_is_civil_second(cls, value: time) -> time:
        return _validate_local_time(value)

    @field_validator("dates")
    @classmethod
    def dates_are_unique_and_sorted(cls, value: tuple[MonthDay, ...]) -> tuple[MonthDay, ...]:
        if not value:
            raise ValueError("yearly cadence requires at least one date selector")
        identities = tuple((item.month, item.day) for item in value)
        if len(set(identities)) != len(identities):
            raise ValueError("yearly dates must be unique")
        return tuple(sorted(value, key=lambda item: (item.month, item.day)))

    @field_validator("timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        return _validate_timezone(value)


class IntervalCadence(BaseModel):
    """ADR-0254: every N minutes, at the multiples of N since the Unix epoch.

    The epoch is the explicit anchor, so the phase depends only on the value.
    The zone only presents the schedule's times; it never moves an instant.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal[CadenceKind.INTERVAL] = CadenceKind.INTERVAL
    every_minutes: int = Field(ge=1, le=1440)
    timezone: str

    @field_validator("timezone")
    @classmethod
    def timezone_is_iana(cls, value: str) -> str:
        return _validate_timezone(value)


type Cadence = (
    OnceCadence | DailyCadence | WeeklyCadence | MonthlyCadence | YearlyCadence | IntervalCadence
)

# ADR-0254: only a schedule that keeps a Cockpit feed current fires on an interval.
FEED_KEEPING_SCOPE = "cockpit.write"


# ADR-0221 bounds: a plan places at most ten calls before any follow-up, all
# within an hour of its first call, never closer together than a minute.
MAX_CALL_PLAN_CALLS = 10
MAX_CALL_PLAN_SPAN = timedelta(minutes=60)


class CallPlan(BaseModel):
    """ADR-0221: retries and a follow-up the platform places for a call schedule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    retry_every_minutes: int | None = Field(default=None, ge=1, le=30)
    retry_until: datetime | None = None
    follow_up_after_answer_minutes: int | None = Field(default=None, ge=1, le=60)

    @field_validator("retry_until")
    @classmethod
    def retry_until_is_aware_utc(cls, value: datetime | None) -> datetime | None:
        return _optional_aware_utc(value)

    @model_validator(mode="after")
    def plan_is_complete(self) -> CallPlan:
        if (self.retry_every_minutes is None) != (self.retry_until is None):
            raise ValueError("call plan retries need both an interval and an end")
        if self.retry_until is None and self.follow_up_after_answer_minutes is None:
            raise ValueError("call plan needs retries, a follow-up, or both")
        return self

    def max_calls(self, first_at: datetime) -> int:
        """Calls before any follow-up: the first, then one per interval in the window."""
        if self.retry_every_minutes is None or self.retry_until is None:
            return 1
        window = self.retry_until - first_at
        return 1 + int(window // timedelta(minutes=self.retry_every_minutes))

    def validate_for(self, first_at: datetime) -> None:
        if self.retry_until is None:
            return
        if self.retry_until <= first_at:
            raise ValueError("call plan retries must end after the first call")
        if self.retry_until - first_at > MAX_CALL_PLAN_SPAN:
            raise ValueError("call plan retries must end within an hour of the first call")
        if self.max_calls(first_at) > MAX_CALL_PLAN_CALLS:
            raise ValueError("call plan places at most ten calls before a follow-up")


class ScheduleState(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class SchedulePauseReason(StrEnum):
    USER = "user"
    FAILURE_LIMIT = "failure_limit"


class ScheduleNotify(StrEnum):
    """When a schedule's finished runs notify the owner (ADR-0225 part D).

    ``every_report`` is the behaviour before the preference existed;
    ``attention_only`` withholds the report notification for a run that
    completed. Approvals, questions, failures and skipped occurrences always
    notify, because each needs the owner.
    """

    EVERY_REPORT = "every_report"
    ATTENTION_ONLY = "attention_only"


class OccurrenceTrigger(StrEnum):
    """Why an occurrence exists: its cadence fired, or the owner asked (ADR-0225 C)."""

    SCHEDULED = "SCHEDULED"
    MANUAL = "MANUAL"


# ADR-0254: a Cockpit refresh is a manual occurrence under its own key, at
# most once a minute per schedule, and never counts toward Run-now's limit.
COCKPIT_REFRESH_KEY_PREFIX = "cockpit.refresh:"
COCKPIT_REFRESH_MIN_AGE = timedelta(minutes=1)


def cockpit_refresh_key(now: datetime) -> str:
    return f"{COCKPIT_REFRESH_KEY_PREFIX}{now.astimezone(UTC):%Y-%m-%dT%H:%M}Z"


def is_cockpit_refresh(manual_request_key: str | None) -> bool:
    return manual_request_key is not None and manual_request_key.startswith(
        COCKPIT_REFRESH_KEY_PREFIX
    )


class ScheduleRefreshOutcome(StrEnum):
    """What a Cockpit refresh did with one feed-keeping schedule (ADR-0254)."""

    STARTED = "started"
    RUNNING = "running"
    RECENT = "recent"
    REFUSED = "refused"


class ScheduleRefresh(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_id: UUID
    outcome: ScheduleRefreshOutcome
    reason: str | None = None


class OccurrenceDisposition(StrEnum):
    MATERIALIZED = "MATERIALIZED"
    MISSED = "MISSED"
    SKIPPED_OVERLAP = "SKIPPED_OVERLAP"
    AUTHORIZATION_FAILED = "AUTHORIZATION_FAILED"
    CONFIGURATION_FAILED = "CONFIGURATION_FAILED"


class ScheduleAdmissionOutcome(StrEnum):
    ALLOW = "ALLOW"
    RETRY = "RETRY"
    REJECT = "REJECT"


class ScheduleAdmissionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: ScheduleAdmissionOutcome
    reason_code: str | None = None

    @model_validator(mode="after")
    def reason_matches_outcome(self) -> ScheduleAdmissionDecision:
        if self.outcome is ScheduleAdmissionOutcome.ALLOW and self.reason_code is not None:
            raise ValueError("allowed admission cannot carry a reason")
        if self.outcome is not ScheduleAdmissionOutcome.ALLOW and not self.reason_code:
            raise ValueError("denied admission requires a stable reason")
        return self


class ScheduleAdmissionLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_active_runs_per_tenant: int = Field(gt=0)
    max_materializations_per_minute: int = Field(gt=0)
    daily_cost: Decimal = Field(gt=0)
    monthly_cost: Decimal = Field(gt=0)


class ScheduleDefinitionLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_run_timeout_seconds: int = Field(gt=0)
    max_misfire_grace_seconds: int = Field(gt=0)
    max_steps_per_run: int = Field(gt=0)
    max_model_calls_per_run: int = Field(gt=0)
    max_tool_calls_per_run: int = Field(gt=0)
    max_cost_per_run: Decimal = Field(gt=0)


class AuthoritySnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    principal: Principal
    authority_version: str = Field(min_length=1)
    enabled: bool = True


class Schedule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    state: ScheduleState
    pause_reason: SchedulePauseReason | None = None
    current_revision: int = Field(ge=1)
    next_fire_at: datetime | None
    consecutive_failures: int = Field(default=0, ge=0)
    created_at: datetime
    updated_at: datetime

    @field_validator("next_fire_at", "created_at", "updated_at")
    @classmethod
    def instants_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return _optional_aware_utc(value)

    @model_validator(mode="after")
    def state_is_consistent(self) -> Schedule:
        if self.state is ScheduleState.PAUSED and self.pause_reason is None:
            raise ValueError("paused schedule requires a pause reason")
        if self.state is not ScheduleState.PAUSED and self.pause_reason is not None:
            raise ValueError("only a paused schedule may carry a pause reason")
        if self.state in {ScheduleState.COMPLETED, ScheduleState.CANCELLED} and (
            self.next_fire_at is not None
        ):
            raise ValueError("terminal schedule must not have a next firing")
        if self.updated_at < self.created_at:
            raise ValueError("schedule updated_at precedes created_at")
        return self


class ScheduleRevision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule_id: UUID
    revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=1024)
    instruction: str = Field(min_length=1, max_length=65_536)
    agent_id: UUID
    agent_version: str = Field(min_length=1)
    policy_profile: str = Field(min_length=1)
    requested_scopes: frozenset[str]
    browser_profile_id: UUID | None = None
    limits: RunLimits
    run_timeout_seconds: int = Field(gt=0)
    cadence: Cadence
    timezone: str | None
    misfire_grace_seconds: int = Field(gt=0)
    max_consecutive_failures: int = Field(gt=0)
    notify: ScheduleNotify = ScheduleNotify.EVERY_REPORT
    created_by_principal_id: str = Field(min_length=1)
    created_at: datetime
    call_plan: CallPlan | None = None

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value)

    @model_validator(mode="after")
    def execution_is_finite_and_cadence_is_consistent(self) -> ScheduleRevision:
        limits = self.limits
        if (
            limits.max_steps <= 0
            or limits.max_model_calls <= 0
            or limits.max_tool_calls <= 0
            or limits.max_cost is None
            or not limits.max_cost.is_finite()
            or limits.max_cost <= Decimal("0")
        ):
            raise ValueError("scheduled run limits must all be finite positive values")
        if limits.deadline_at is not None:
            raise ValueError("scheduled revision deadline is derived at materialization")
        if isinstance(self.cadence, OnceCadence):
            if self.timezone is not None:
                raise ValueError("one-time cadence has no timezone")
        elif self.timezone != self.cadence.timezone:
            raise ValueError("cadence timezone must match revision timezone")
        check_call_plan(self.call_plan, self.cadence)
        return self


class ScheduleDefinition(BaseModel):
    """Complete user-controlled definition used for create and revision updates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str = Field(min_length=1, max_length=1024)
    instruction: str = Field(min_length=1, max_length=65_536)
    agent_id: UUID
    agent_version: str = Field(min_length=1)
    policy_profile: str = Field(min_length=1)
    requested_scopes: frozenset[str]
    browser_profile_id: UUID | None = None
    limits: RunLimits
    run_timeout_seconds: int = Field(gt=0)
    cadence: Cadence
    misfire_grace_seconds: int = Field(gt=0)
    max_consecutive_failures: int = Field(gt=0)
    call_plan: CallPlan | None = None
    notify: ScheduleNotify = ScheduleNotify.EVERY_REPORT

    @model_validator(mode="after")
    def execution_is_finite(self) -> ScheduleDefinition:
        limits = self.limits
        if (
            limits.max_steps <= 0
            or limits.max_model_calls <= 0
            or limits.max_tool_calls <= 0
            or limits.max_cost is None
            or not limits.max_cost.is_finite()
            or limits.max_cost <= Decimal("0")
        ):
            raise ValueError("scheduled run limits must all be finite positive values")
        if limits.deadline_at is not None:
            raise ValueError("scheduled definition deadline is derived at materialization")
        check_call_plan(self.call_plan, self.cadence)
        return self

    @property
    def timezone(self) -> str | None:
        if isinstance(self.cadence, OnceCadence):
            return None
        return self.cadence.timezone


class ScheduleDefinitionPatch(BaseModel):
    """Model-visible definition fields allowed to change conversationally."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=1024)
    instruction: str | None = Field(default=None, min_length=1, max_length=65_536)
    cadence: Cadence | None = None

    @model_validator(mode="after")
    def at_least_one_field_changes(self) -> ScheduleDefinitionPatch:
        if self.title is None and self.instruction is None and self.cadence is None:
            raise ValueError("schedule update requires at least one changed field")
        return self


class ScheduleRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schedule: Schedule
    revision: ScheduleRevision
    replayed: bool = False


class ScheduleOccurrence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: UUID
    schedule_id: UUID
    schedule_revision: int = Field(ge=1)
    nominal_fire_at: datetime
    disposition: OccurrenceDisposition
    session_id: UUID | None = None
    run_id: UUID | None = None
    reason_code: str | None = None
    authority_version: str | None = None
    materialized_at: datetime | None = None
    links_erased_at: datetime | None = None
    created_at: datetime
    # ADR-0225 part C: a manual occurrence's nominal instant is the request
    # time, and its idempotency key is unique per schedule.
    trigger: OccurrenceTrigger = OccurrenceTrigger.SCHEDULED
    manual_request_key: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("nominal_fire_at", "materialized_at", "links_erased_at", "created_at")
    @classmethod
    def occurrence_instants_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        return _optional_aware_utc(value)

    @model_validator(mode="after")
    def manual_key_matches_trigger(self) -> ScheduleOccurrence:
        if (self.trigger is OccurrenceTrigger.MANUAL) != (self.manual_request_key is not None):
            raise ValueError("only a manual occurrence carries a request key, and it must")
        if self.trigger is OccurrenceTrigger.MANUAL and (
            self.disposition is not OccurrenceDisposition.MATERIALIZED
        ):
            raise ValueError("a manual occurrence is recorded only once it starts a run")
        return self

    @model_validator(mode="after")
    def disposition_is_consistent(self) -> ScheduleOccurrence:
        if self.disposition is OccurrenceDisposition.MATERIALIZED:
            if self.authority_version is None or self.materialized_at is None:
                raise ValueError("materialized occurrence requires authority and time")
            if self.links_erased_at is None:
                if self.session_id is None or self.run_id is None:
                    raise ValueError("materialized occurrence requires session and run links")
            elif self.session_id is not None or self.run_id is not None:
                raise ValueError("erased materialized occurrence must clear both links")
            if self.reason_code is not None:
                raise ValueError("materialized occurrence cannot carry a reason code")
            if self.materialized_at < self.nominal_fire_at:
                raise ValueError("materialized occurrence cannot precede its nominal instant")
            if self.links_erased_at is not None and self.links_erased_at < self.materialized_at:
                raise ValueError("materialized links cannot be erased before materialization")
        else:
            if (
                self.session_id is not None
                or self.run_id is not None
                or self.materialized_at is not None
                or self.links_erased_at is not None
            ):
                raise ValueError(
                    "non-materialized occurrence cannot carry materialization or erasure state"
                )
            if self.reason_code is None or not self.reason_code.strip():
                raise ValueError("non-materialized occurrence requires a stable reason code")
        return self


class ScheduledRunLink(BaseModel):
    """Internal ownership link from a materialized occurrence to its run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    occurrence: ScheduleOccurrence
    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def occurrence_has_a_run(self) -> ScheduledRunLink:
        if self.occurrence.run_id is None:
            raise ValueError("scheduled run link requires an occurrence run")
        return self


class ScheduleIdempotencyRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tenant_id: str = Field(min_length=1)
    principal_id: str = Field(min_length=1)
    key: str = Field(min_length=1, max_length=256)
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    schedule_id: UUID
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def created_at_is_aware_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value)


class ScheduleCursor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    updated_at: datetime
    id: UUID

    @field_validator("updated_at")
    @classmethod
    def updated_at_is_aware_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value)


class OccurrenceCursor(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    nominal_fire_at: datetime
    id: UUID

    @field_validator("nominal_fire_at")
    @classmethod
    def nominal_fire_at_is_aware_utc(cls, value: datetime) -> datetime:
        return _aware_utc(value)


def check_interval_cadence(cadence: Cadence, requested_scopes: frozenset[str]) -> None:
    """ADR-0254: an interval is for a feed-keeping schedule alone."""
    if isinstance(cadence, IntervalCadence) and FEED_KEEPING_SCOPE not in requested_scopes:
        raise ValueError("an interval cadence keeps a Cockpit feed; request publish_cockpit_feed")


def check_call_plan(plan: CallPlan | None, cadence: Cadence) -> None:
    """A call plan follows one first call, so only a one-time schedule carries one.

    Raises ValueError so model validation and the schedule service share it.
    """
    if plan is None:
        return
    if not isinstance(cadence, OnceCadence):
        raise ValueError("a call plan requires a one-time cadence")
    plan.validate_for(cadence.at)


def _validate_local_time(value: time) -> time:
    if value.tzinfo is not None:
        raise ValueError("local_time must not carry a UTC offset")
    if value.microsecond:
        raise ValueError("local_time must have whole-second precision")
    return value


def _validate_timezone(value: str) -> str:
    if not value or value != value.strip():
        raise ValueError("timezone must be an IANA name")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("timezone must be an IANA name") from exc
    return value


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("schedule instants must be aware")
    return value.astimezone(UTC)


def _optional_aware_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _aware_utc(value)


class ScheduleDraftStatus(StrEnum):
    """What a plain-words draft came to (ADR-0225 part B)."""

    READY = "ready"
    NEEDS_CLARIFICATION = "needs_clarification"
    FAILED = "failed"


class ScheduleDraft(BaseModel):
    """A schedule read from the owner's words, never persisted.

    `definition` is complete when `status` is ready: the owner's sentence as
    the instruction, the parsed cadence, and every pin the chat tool derives.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: ScheduleDraftStatus
    reason: str | None = None
    definition: ScheduleDefinition | None = None
    next_fire_at: list[datetime] = Field(default_factory=list)
