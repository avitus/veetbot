"""The platform's `veetbot.*` feed sources (Milestone 35).

docs/plan/cockpit.md, platform feeds. Each source reads one subsystem as the
owner, through the same repositories its routes read, and returns the feed's
data. Times are absolute ISO 8601 instants, so unchanged state projects to
unchanged content and the projector's change test holds; widgets compute ages
with the kit's clock. Each source keeps to stated bounds and leaves out what is
another principal's, expired, erased or unsafe to show. A source is present
only when its feature is enabled.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent_core.application.email import read_value, records, thread_summaries
from agent_core.domain.agents import Principal
from agent_core.domain.approvals import ApprovalCursor, ApprovalRequest
from agent_core.domain.cockpit import FeedSchemaDocument
from agent_core.domain.email import (
    EmailDraft,
    EmailDraftStatus,
    EmailFeedback,
    EmailThread,
    apply_feedback,
)
from agent_core.domain.hazards import contains_injection_pattern, contains_secret_material
from agent_core.domain.memory import Sensitivity
from agent_core.domain.people import PeopleInteraction, PeopleQuery, Person
from agent_core.domain.policies import ActionKind
from agent_core.domain.runs import Run, RunStatus
from agent_core.domain.schedules import (
    OccurrenceDisposition,
    Schedule,
    ScheduleRevision,
    ScheduleState,
)
from agent_core.ports.persistence import RepositoryUnitOfWork

APPROVALS_FEED = "veetbot.approvals.pending"
EMAIL_FEED = "veetbot.email.priority"
SCHEDULES_FEED = "veetbot.schedules.health"
CALLS_FEED = "veetbot.calls.recent"
PEOPLE_FEED = "veetbot.people.recent"
RUNS_FEED = "veetbot.runs.activity"

_TEXT = {"type": "string", "maxLength": 200}
# An identifier a widget hands back to open its destination natively.
_IDENTIFIER = {"type": "string", "maxLength": 64}
_INSTANT = {"type": "string", "format": "date-time"}
_COUNT = {"type": "integer", "minimum": 0}
_DAY = timedelta(days=1)
_PAGE = 100
# The scans behind a feed stop here; an owner with more is shown the newest.
_SCAN_PAGES = 20


def _instant(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _shown(text: str | None, limit: int = 200) -> str:
    return (text or "").strip()[:limit]


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": sorted(properties),
        "additionalProperties": False,
    }


def _feed(properties: dict[str, Any], item: dict[str, Any], max_items: int) -> FeedSchemaDocument:
    return _object({**properties, "items": {"type": "array", "maxItems": max_items, "items": item}})


# Approvals --------------------------------------------------------------------

_APPROVAL_ITEMS = 10
_APPROVAL_KINDS = {
    "schedule": "Schedule",
    "browser": "Website",
    "device": "Text message",
    "email": "Email",
    "cockpit": "Cockpit",
    "records": "Records",
    "people": "People",
    "artifact": "Export",
    "skill": "Skill",
    "memory": "Memory",
    "workspace": "Workspace",
    "sandbox": "Sandbox",
    "delegate": "Delegation",
    "image": "Image",
    "video": "Video",
    "demo": "Demo",
}
_ACTION_KINDS = {
    ActionKind.MEMORY_WRITE: "Memory",
    ActionKind.SKILL_AUTHORING: "Skill",
    ActionKind.ARTIFACT_EXPORT: "Export",
}


def _approval_kind(approval: ApprovalRequest) -> str:
    """A short platform label for what the approval would do; never request text."""

    if approval.action_kind in _ACTION_KINDS:
        return _ACTION_KINDS[approval.action_kind]
    name = approval.tool_name or ""
    domain, _, rest = name.partition(".")
    if domain == "mcp":
        server = rest.partition(".")[0]
        if server.startswith("gmail"):
            return "Email"
        if server.startswith("bland"):
            return "Call"
        return "Connector"
    return _APPROVAL_KINDS.get(domain, "Action")


class ApprovalsSource:
    """The count and up to ten of the owner's pending approvals, oldest first."""

    feed_id = APPROVALS_FEED
    schema: FeedSchemaDocument = _feed(
        {"count": _COUNT, "oldestAt": {"anyOf": [_INSTANT, {"type": "null"}]}},
        _object(
            {
                "id": _IDENTIFIER,
                "sessionId": {"anyOf": [_IDENTIFIER, {"type": "null"}]},
                "kind": _TEXT,
                "summary": {"type": "string", "maxLength": 300},
                "requestedAt": _INSTANT,
                "expiresAt": {"anyOf": [_INSTANT, {"type": "null"}]},
                "risk": {"enum": ["low", "medium", "high", "critical"]},
            }
        ),
        _APPROVAL_ITEMS,
    )

    async def read(
        self, uow: RepositoryUnitOfWork, principal: Principal, now: datetime
    ) -> dict[str, Any]:
        pending: list[ApprovalRequest] = []
        cursor: ApprovalCursor | None = None
        for _ in range(_SCAN_PAGES):
            page = await uow.approvals.list_pending(principal, limit=_PAGE, cursor=cursor)
            # The repositories scope approvals by tenant; the owner's are this principal's.
            pending.extend(
                row
                for row in page
                if row.principal_id == principal.principal_id
                and (row.expires_at is None or row.expires_at > now)
            )
            if len(page) < _PAGE:
                break
            cursor = ApprovalCursor(created_at=page[-1].created_at, id=page[-1].id)
        pending.sort(key=lambda row: (row.created_at, row.id))
        return {
            "count": len(pending),
            "oldestAt": _instant(pending[0].created_at) if pending else None,
            "items": [
                {
                    "id": str(row.id),
                    "sessionId": None if row.session_id is None else str(row.session_id),
                    "kind": _approval_kind(row),
                    "summary": _shown(row.action_summary, 300),
                    "requestedAt": _instant(row.created_at),
                    "expiresAt": None if row.expires_at is None else _instant(row.expires_at),
                    "risk": row.risk.value,
                }
                for row in pending[:_APPROVAL_ITEMS]
            ],
        }


# Email -------------------------------------------------------------------------

_EMAIL_ITEMS = 5
_PRIORITY = 0.7
_DRAFTED = frozenset({EmailDraftStatus.READY, EmailDraftStatus.AWAITING_APPROVAL})


def _correspondent(senders: Sequence[str]) -> str:
    if not senders:
        return ""
    name, address = parseaddr(senders[0])
    return _shown(name or address or senders[0])


class EmailPrioritySource:
    """Up to five priority threads, ranked as the Email mode's priority view ranks them."""

    feed_id = EMAIL_FEED
    schema: FeedSchemaDocument = _feed(
        {"count": _COUNT},
        _object(
            {
                "threadId": _IDENTIFIER,
                "correspondent": _TEXT,
                "subject": _TEXT,
                "state": {"enum": ["needs_reply", "drafted", "fyi"]},
                "updatedAt": _INSTANT,
            }
        ),
        _EMAIL_ITEMS,
    )

    def __init__(self, *, account_ids: Sequence[str]) -> None:
        self._accounts = frozenset(account_ids)

    async def read(
        self, uow: RepositoryUnitOfWork, principal: Principal, now: datetime
    ) -> dict[str, Any]:
        feedback = [
            EmailFeedback.model_validate(row.payload)
            async for row in records(uow.email, principal, "feedback")
        ]
        priority: list[EmailThread] = []
        async for stored in thread_summaries(uow.email, principal):
            if stored.account_id not in self._accounts:
                continue
            thread = apply_feedback(stored, feedback, now=now)
            if (
                thread.in_inbox
                and thread.priority >= _PRIORITY
                and thread.dismissed_revision != thread.revision
            ):
                priority.append(thread)
        priority.sort(key=lambda item: (-item.priority, -item.updated_at.timestamp(), str(item.id)))
        items = []
        for thread in priority[:_EMAIL_ITEMS]:
            items.append(
                {
                    "threadId": str(thread.id),
                    "correspondent": _correspondent(thread.senders),
                    "subject": _shown(thread.subject),
                    "state": await self._state(uow, principal, thread),
                    "updatedAt": _instant(thread.updated_at),
                }
            )
        return {"count": len(priority), "items": items}

    @staticmethod
    async def _state(uow: RepositoryUnitOfWork, principal: Principal, thread: EmailThread) -> str:
        if thread.draft_id is not None:
            draft = await read_value(
                uow.email, principal, "draft", str(thread.draft_id), EmailDraft
            )
            if draft is not None and draft.status in _DRAFTED:
                return "drafted"
        return "needs_reply" if thread.needs_reply else "fyi"


# Schedules ----------------------------------------------------------------------

_SCHEDULE_ITEMS = 12
_SCHEDULE_SCAN = 100
_OCCURRENCES_PER_SCHEDULE = 24
_SETTLED = {
    RunStatus.COMPLETED: ("done", "Done"),
    RunStatus.FAILED: ("failed", "Failed"),
    RunStatus.CANCELLED: ("failed", "Cancelled"),
}
_UNSTARTED = {
    OccurrenceDisposition.MISSED: ("attention", "Missed"),
    OccurrenceDisposition.SKIPPED_OVERLAP: ("attention", "Skipped"),
    OccurrenceDisposition.AUTHORIZATION_FAILED: ("failed", "Not authorized"),
    OccurrenceDisposition.CONFIGURATION_FAILED: ("failed", "Not configured"),
}


def _zone(revision: ScheduleRevision | None) -> ZoneInfo:
    name = None
    if revision is not None:
        name = revision.timezone or getattr(revision.cadence, "timezone", None)
    try:
        return ZoneInfo(name or "UTC")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("UTC")


def _clock(value: datetime, zone: ZoneInfo) -> str:
    local = value.astimezone(zone)
    return f"{local.hour}:{local.minute:02d}"


class SchedulesSource:
    """The last day's scheduled runs, each active schedule's next run, and paused schedules."""

    feed_id = SCHEDULES_FEED
    schema: FeedSchemaDocument = _feed(
        {"onTime": _COUNT, "total": _COUNT, "failed": _COUNT, "paused": _COUNT},
        _object(
            {
                "id": {"type": "string", "maxLength": 64},
                "scheduleId": _IDENTIFIER,
                "time": {"type": "string", "maxLength": 8},
                "title": _TEXT,
                "detail": {"type": "string", "maxLength": 40},
                "state": {"enum": ["done", "now", "next", "failed", "attention"]},
                "at": {"anyOf": [_INSTANT, {"type": "null"}]},
            }
        ),
        _SCHEDULE_ITEMS,
    )

    async def read(
        self, uow: RepositoryUnitOfWork, principal: Principal, now: datetime
    ) -> dict[str, Any]:
        schedules: list[Schedule] = await uow.schedules.list(
            principal,
            limit=_SCHEDULE_SCAN,
            states=frozenset({ScheduleState.ACTIVE, ScheduleState.PAUSED}),
        )
        revisions = await uow.schedules.get_revisions(
            tuple((schedule.id, schedule.current_revision) for schedule in schedules), principal
        )
        dated: list[tuple[datetime, dict[str, Any]]] = []
        paused: list[dict[str, Any]] = []
        counts = {"onTime": 0, "total": 0, "failed": 0}
        for schedule in schedules:
            revision = revisions.get((schedule.id, schedule.current_revision))
            title = _shown(revision.title if revision is not None else "Schedule")
            zone = _zone(revision)
            if schedule.state is ScheduleState.PAUSED:
                paused.append(
                    self._item(
                        f"paused-{schedule.id}",
                        schedule.id,
                        "—",
                        title,
                        "Paused",
                        "attention",
                        None,
                    )
                )
                continue
            for occurrence in await uow.schedule_occurrences.list(
                schedule.id, principal, limit=_OCCURRENCES_PER_SCHEDULE
            ):
                if occurrence.nominal_fire_at < now - _DAY or occurrence.nominal_fire_at > now:
                    continue
                state, detail = await self._outcome(uow, principal, occurrence)
                counts["total"] += 1
                counts["onTime"] += int(state == "done")
                counts["failed"] += int(state == "failed")
                at = occurrence.nominal_fire_at
                dated.append(
                    (
                        at,
                        self._item(
                            str(occurrence.id),
                            schedule.id,
                            _clock(at, zone),
                            title,
                            detail,
                            state,
                            at,
                        ),
                    )
                )
            if schedule.next_fire_at is not None:
                at = schedule.next_fire_at
                dated.append(
                    (
                        at,
                        self._item(
                            f"next-{schedule.id}",
                            schedule.id,
                            _clock(at, zone),
                            title,
                            "Next",
                            "next",
                            at,
                        ),
                    )
                )
        dated.sort(key=lambda pair: (pair[0], pair[1]["id"]))
        items = [item for _at, item in dated] + paused
        return {**counts, "paused": len(paused), "items": items[:_SCHEDULE_ITEMS]}

    @staticmethod
    def _item(
        identifier: str,
        schedule_id: UUID,
        time: str,
        title: str,
        detail: str,
        state: str,
        at: datetime | None,
    ) -> dict[str, Any]:
        return {
            "id": identifier,
            "scheduleId": str(schedule_id),
            "time": time,
            "title": title,
            "detail": detail,
            "state": state,
            "at": None if at is None else _instant(at),
        }

    @staticmethod
    async def _outcome(
        uow: RepositoryUnitOfWork, principal: Principal, occurrence: Any
    ) -> tuple[str, str]:
        if occurrence.disposition in _UNSTARTED:
            return _UNSTARTED[occurrence.disposition]
        if occurrence.run_id is None:
            return "done", "Done"
        run: Run = await uow.runs.get(occurrence.run_id, principal)
        return _SETTLED.get(run.status, ("now", "Running"))


# Calls --------------------------------------------------------------------------

_CALL_ITEMS = 5
_CALL_RETENTION = timedelta(days=30)


class CallsSource:
    """The five newest visible calls with their status and outcome summary."""

    feed_id = CALLS_FEED
    schema: FeedSchemaDocument = _feed(
        {},
        _object(
            {
                "direction": {"enum": ["inbound", "outbound"]},
                "counterparty": {"type": "string", "maxLength": 32},
                "status": {"type": "string", "maxLength": 32},
                "summary": {"anyOf": [_TEXT, {"type": "null"}]},
                "startedAt": _INSTANT,
                "durationMinutes": {"anyOf": [{"type": "number", "minimum": 0}, {"type": "null"}]},
            }
        ),
        _CALL_ITEMS,
    )

    async def read(
        self, uow: RepositoryUnitOfWork, principal: Principal, now: datetime
    ) -> dict[str, Any]:
        visible: list[tuple[datetime, dict[str, Any]]] = []
        after: str | None = None
        for _ in range(_SCAN_PAGES):
            page = await uow.calls.list(principal, "call", after=after, limit=1000)
            for row in page:
                payload = row.payload
                created = payload.get("created_at")
                if payload.get("erased") or payload.get("status") == "erased":
                    continue
                if not isinstance(created, str):
                    continue
                started = datetime.fromisoformat(created)
                if started < now - _CALL_RETENTION:
                    continue
                visible.append((started, self._entry(payload, started)))
            if len(page) < 1000:
                break
            after = page[-1].key
        visible.sort(key=lambda pair: pair[0], reverse=True)
        return {"items": [entry for _started, entry in visible[:_CALL_ITEMS]]}

    @staticmethod
    def _entry(payload: dict[str, Any], started: datetime) -> dict[str, Any]:
        duration = payload.get("duration_minutes")
        summary = payload.get("summary")
        direction = payload.get("direction")
        return {
            "direction": direction if direction in {"inbound", "outbound"} else "outbound",
            "counterparty": _shown(str(payload.get("counterparty") or ""), 32),
            "status": _shown(str(payload.get("status") or "unknown"), 32),
            "summary": _shown(summary) if isinstance(summary, str) and summary.strip() else None,
            "startedAt": _instant(started),
            "durationMinutes": duration
            if isinstance(duration, int | float)
            and not isinstance(duration, bool)
            and duration >= 0
            else None,
        }


# People -------------------------------------------------------------------------

_PEOPLE_ITEMS = 5
_PEOPLE_CEILING = Sensitivity.SENSITIVE


def _safe_name(name: str) -> bool:
    return not contains_injection_pattern(name) and not contains_secret_material(name)


class PeopleSource:
    """The five people most recently in touch, with their last interaction's kind."""

    feed_id = PEOPLE_FEED
    schema: FeedSchemaDocument = _feed(
        {},
        _object(
            {
                "name": _TEXT,
                "lastAt": _INSTANT,
                "kind": {"type": "string", "maxLength": 32},
                "channel": {"type": "string", "maxLength": 16},
            }
        ),
        _PEOPLE_ITEMS,
    )

    async def read(
        self, uow: RepositoryUnitOfWork, principal: Principal, now: datetime
    ) -> dict[str, Any]:
        rows = await uow.people.query(
            PeopleQuery(
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                kinds=["person"],
                sort="recent",
                states=["active", "provisional"],
                sensitivity_ceiling=_PEOPLE_CEILING,
                as_of=now,
                limit=_PEOPLE_ITEMS * 2,
            )
        )
        people = [row for row in rows if isinstance(row, Person) and _safe_name(row.display_name)]
        times = await uow.people.last_interactions(
            principal, [person.id for person in people], ceiling=_PEOPLE_CEILING, as_of=now
        )
        entries: list[tuple[datetime, dict[str, Any]]] = []
        for person in people:
            last = times.get(person.id)
            if last is None:
                continue
            latest = await uow.people.query(
                PeopleQuery(
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                    kinds=["interaction"],
                    person_id=person.id,
                    sort="history",
                    unknown_time="exclude",
                    until=now,
                    sensitivity_ceiling=_PEOPLE_CEILING,
                    limit=1,
                )
            )
            interaction = next((row for row in latest if isinstance(row, PeopleInteraction)), None)
            entries.append(
                (
                    last,
                    {
                        "name": _shown(person.display_name),
                        "lastAt": _instant(last),
                        "kind": "" if interaction is None else str(interaction.interaction_kind),
                        "channel": "" if interaction is None else str(interaction.channel),
                    },
                )
            )
        entries.sort(key=lambda pair: pair[0], reverse=True)
        return {"items": [entry for _last, entry in entries[:_PEOPLE_ITEMS]]}


# Runs ---------------------------------------------------------------------------

_RUN_ITEMS = 8
_SESSION_SCAN = 100
_ACTIVE = {
    RunStatus.QUEUED: "running",
    RunStatus.RUNNING: "running",
    RunStatus.WAITING_FOR_APPROVAL: "waiting_for_approval",
    RunStatus.WAITING_FOR_USER: "waiting_for_user",
}


class RunsSource:
    """Running and waiting conversations, and those whose run failed in the last day."""

    feed_id = RUNS_FEED
    schema: FeedSchemaDocument = _feed(
        {"running": _COUNT, "waiting": _COUNT, "failed": _COUNT},
        _object(
            {
                "sessionId": _IDENTIFIER,
                "title": _TEXT,
                "state": {
                    "enum": ["running", "waiting_for_approval", "waiting_for_user", "failed"]
                },
                "since": _INSTANT,
            }
        ),
        _RUN_ITEMS,
    )

    async def read(
        self, uow: RepositoryUnitOfWork, principal: Principal, now: datetime
    ) -> dict[str, Any]:
        sessions = await uow.sessions.list(principal, limit=_SESSION_SCAN, exclude_operational=True)
        latest: dict[UUID, Run] = await uow.runs.latest_for_sessions(
            [session.id for session in sessions], principal
        )
        counts = {"running": 0, "waiting": 0, "failed": 0}
        entries: list[tuple[datetime, dict[str, Any]]] = []
        for session in sessions:
            run = latest.get(session.id)
            if run is None:
                continue
            if run.status in _ACTIVE:
                state, since = _ACTIVE[run.status], run.created_at
                counts["running" if state == "running" else "waiting"] += 1
            elif run.status is RunStatus.FAILED:
                since = run.failure.occurred_at if run.failure is not None else run.updated_at
                if since < now - _DAY:
                    continue
                state = "failed"
                counts["failed"] += 1
            else:
                continue
            title = _shown(session.title) or "Untitled chat"
            entries.append(
                (
                    since,
                    {
                        "sessionId": str(session.id),
                        "title": title,
                        "state": state,
                        "since": _instant(since),
                    },
                )
            )
        entries.sort(key=lambda pair: pair[0], reverse=True)
        return {**counts, "items": [entry for _since, entry in entries[:_RUN_ITEMS]]}


type PlatformSource = (
    ApprovalsSource
    | EmailPrioritySource
    | SchedulesSource
    | CallsSource
    | PeopleSource
    | RunsSource
)


def platform_feed_sources(
    *,
    email_account_ids: Sequence[str] = (),
    schedules: bool = False,
    calls: bool = False,
    people: bool = False,
) -> list[PlatformSource]:
    """The sources whose features this deployment enables, in a stable order."""

    sources: list[PlatformSource] = [ApprovalsSource()]
    if email_account_ids:
        sources.append(EmailPrioritySource(account_ids=email_account_ids))
    if schedules:
        sources.append(SchedulesSource())
    if calls:
        sources.append(CallsSource())
    if people:
        sources.append(PeopleSource())
    sources.append(RunsSource())
    return sources
