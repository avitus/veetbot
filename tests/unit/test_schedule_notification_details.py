"""Authorized schedule identity stays pinned, bounded, and compatible with old clients."""

import json
from datetime import time
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
import pytest
from pydantic import ValidationError

from agent_core.adapters.apns import APNsPushTransport
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.application.notification_producer import NotificationProducer
from agent_core.domain.devices import PushEnvironment
from agent_core.domain.notifications import NotificationPayload
from agent_core.domain.runs import RunStatus
from agent_core.domain.schedules import (
    DailyCadence,
    OccurrenceDisposition,
    OnceCadence,
    ScheduleOccurrence,
)
from agent_core.scheduling.accounting import ScheduleOutcomeAccountant
from tests.contract.support import NOW, RUN_ID, SESSION_ID, memory_uow_factory, principal, run
from tests.contract.test_push_transport_contract import push_message, push_target
from tests.contract.test_schedule_repository_contract import revision, schedule
from tests.unit.test_apns_m12 import _private_key_file


def payload_values() -> dict[str, object]:
    return {
        "kind": "schedule_run_finished",
        "title": "Scheduled run finished",
        "status": "COMPLETED",
        "session_id": SESSION_ID,
        "run_id": RUN_ID,
        "schedule_id": UUID(int=800),
        "occurrence_id": UUID(int=801),
        "notification_id": UUID(int=802),
        "schedule_context": {
            "title": "Morning inbox triage",
            "scheduled_for": "2026-09-11T08:00:00-07:00",
        },
    }


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED])
@pytest.mark.parametrize("zone", [None, "America/Los_Angeles"])
async def test_accounting_snapshots_the_occurrence_revision_after_a_schedule_rename(
    status: RunStatus,
    zone: str | None,
) -> None:
    clock, factory = await memory_uow_factory()
    ids = SequenceIdFactory()
    initial = schedule()
    pinned = revision().model_copy(
        update={
            "title": "Original daily briefing",
            "instruction": "Private instruction not for a push",
            "cadence": OnceCadence(at=NOW)
            if zone is None
            else DailyCadence(local_time=time(6), timezone=zone),
            "timezone": zone,
        }
    )
    occurrence = ScheduleOccurrence(
        id=UUID(int=830),
        schedule_id=initial.id,
        schedule_revision=1,
        nominal_fire_at=NOW,
        disposition=OccurrenceDisposition.MATERIALIZED,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        authority_version="authority-v1",
        materialized_at=NOW,
        created_at=NOW,
    )
    async with factory() as uow:
        await uow.schedules.create(initial, pinned)
        await uow.runs.create(
            run(status=status).model_copy(update={"final_message": "Private result"})
        )
        await uow.schedule_occurrences.insert(occurrence)
        await uow.schedules.replace(
            initial,
            initial.model_copy(update={"current_revision": 2}),
            pinned.model_copy(update={"revision": 2, "title": "New unrelated schedule name"}),
        )
    accountant = ScheduleOutcomeAccountant(
        uow_factory=factory,
        clock=clock,
        ids=ids,
        notification_producer=NotificationProducer(clock=clock, ids=ids),
    )
    assert await accountant.account(RUN_ID)
    assert not await accountant.account(RUN_ID)
    async with factory() as uow:
        [notification] = await uow.notification_outbox.list(principal(), limit=10)
        events = await uow.process_events.list()
    assert notification.payload.model_dump(mode="json").get("schedule_context") == {
        "title": "Original daily briefing",
        "scheduled_for": NOW.astimezone(ZoneInfo(zone or "UTC")).isoformat().replace("+00:00", "Z"),
    }
    serialized = notification.payload.model_dump_json()
    assert "Private" not in serialized
    assert "New unrelated" not in serialized
    assert all("Original daily briefing" not in event.model_dump_json() for event in events)
    assert notification.payload.status == status


@pytest.mark.parametrize(
    "title", ["Morning inbox triage", "🧪" * 1024], ids=["title", "unicode-limit"]
)
async def test_apns_shows_schedule_identity_and_time_but_keeps_the_v1_tap_dictionary(
    tmp_path: Path,
    title: str,
) -> None:
    values = payload_values()
    values["schedule_context"] = {"title": title, "scheduled_for": "2026-09-11T08:00:00-07:00"}
    payload = NotificationPayload.model_validate(values)
    requests: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    key_path, _ = _private_key_file(tmp_path)
    transport = APNsPushTransport(
        key_file=key_path,
        key_id="KEY123",
        team_id="TEAM123",
        topic="com.veetbot.app",
        clock=FixedClock(NOW),
        clients={
            PushEnvironment.SANDBOX: httpx.AsyncClient(
                base_url="https://api.sandbox.push.apple.com:443",
                transport=httpx.MockTransport(record),
            )
        },
    )
    try:
        await transport.deliver(
            push_target(), push_message().model_copy(update={"payload": payload})
        )
        wire = json.loads(requests[0].content)
        assert wire["aps"]["alert"] == {
            "title": "Scheduled run completed",
            "subtitle": title if len(title) <= 160 else title[:159] + "…",
            "body": "Scheduled for Sep 11, 2026 at 08:00 UTC-07:00. "
            "The scheduled task finished successfully. Open Veetbot to view the result.",
        }
        assert wire["veetbot"] == payload.model_dump(
            mode="json", exclude_none=True, exclude={"schedule_context"}
        )
        assert "schedule_context" not in wire["veetbot"]
        assert len(json.dumps(wire, ensure_ascii=True).encode()) < 4096
    finally:
        await transport.aclose()


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("  Morning\n inbox\ttriage\u202e ", "Morning inbox triage"),
        ("\n\t", "Scheduled task"),
        ("x" * 170 + " api_key=" + "unsafe" * 4, "Scheduled task"),
        ("pass\u200bword=example-value", "Scheduled task"),
    ],
)
def test_schedule_title_is_filtered_before_truncation(title: str, expected: str) -> None:
    values = payload_values()
    values["schedule_context"] = {"title": title, "scheduled_for": NOW}
    payload = NotificationPayload.model_validate(values)
    assert payload.model_dump()["schedule_context"]["title"] == expected
    assert expected not in repr(payload)


def test_schedule_context_remains_closed_and_requires_an_aware_instant() -> None:
    for extra in (
        {"instruction": "private"},
        {"result": "private"},
        {"scheduled_for": "2026-09-11T08:00:00"},
    ):
        values = payload_values()
        values["schedule_context"] = {"title": "Briefing", "scheduled_for": NOW, **extra}
        with pytest.raises(ValidationError):
            NotificationPayload.model_validate(values)


def test_schedule_context_is_forbidden_on_other_kinds_and_legacy_rows_still_decode() -> None:
    values = payload_values()
    context = values.pop("schedule_context")
    legacy = NotificationPayload.model_validate(values)
    assert legacy.model_dump().get("schedule_context") is None
    for kind, title in (("test", "Test notification"), ("run_failed", "Run failed")):
        base = {
            "kind": kind,
            "title": title,
            "notification_id": UUID(int=802),
            "schedule_context": context,
        }
        if kind == "run_failed":
            base.update(status="FAILED", session_id=SESSION_ID, run_id=RUN_ID)
        with pytest.raises(ValidationError):
            NotificationPayload.model_validate(base)


@pytest.mark.parametrize(
    "disposition",
    [
        OccurrenceDisposition.MISSED,
        OccurrenceDisposition.SKIPPED_OVERLAP,
        OccurrenceDisposition.AUTHORIZATION_FAILED,
        OccurrenceDisposition.CONFIGURATION_FAILED,
    ],
)
async def test_skipped_occurrences_snapshot_identity_and_retries_keep_it(
    disposition: OccurrenceDisposition,
) -> None:
    clock, factory = await memory_uow_factory()
    producer = NotificationProducer(clock=clock, ids=SequenceIdFactory())
    pinned = revision().model_copy(update={"title": "Weekly report"})
    occurrence = ScheduleOccurrence(
        id=UUID(int=840),
        schedule_id=schedule().id,
        schedule_revision=1,
        nominal_fire_at=NOW,
        disposition=disposition,
        reason_code="schedule.test_skip",
        created_at=NOW,
    )
    async with factory() as uow:
        await uow.schedules.create(schedule(), pinned)
        await uow.schedule_occurrences.insert(occurrence)
        assert await producer.for_schedule_occurrence(
            uow,
            schedule=schedule(),
            occurrence=occurrence,
            revision=pinned,
        )
        assert not await producer.for_schedule_occurrence(
            uow,
            schedule=schedule(),
            occurrence=occurrence,
            revision=pinned,
        )
    async with factory() as uow:
        [notification] = await uow.notification_outbox.list(principal(), limit=10)
    context = notification.payload.schedule_context
    assert context is not None
    assert context.title == "Weekly report"
    assert context.scheduled_for == NOW
    assert notification.payload.status == disposition


@pytest.mark.parametrize("mismatch", [{"revision": 2}, {"schedule_id": UUID(int=999)}])
async def test_mismatched_revision_is_audited_without_disclosing_its_title(
    mismatch: dict[str, object],
) -> None:
    clock, factory = await memory_uow_factory()
    producer = NotificationProducer(clock=clock, ids=SequenceIdFactory())
    occurrence = ScheduleOccurrence(
        id=UUID(int=841),
        schedule_id=schedule().id,
        schedule_revision=1,
        nominal_fire_at=NOW,
        disposition=OccurrenceDisposition.MISSED,
        reason_code="schedule.test_skip",
        created_at=NOW,
    )
    async with factory() as uow:
        assert not await producer.for_schedule_occurrence(
            uow,
            schedule=schedule(),
            occurrence=occurrence,
            revision=revision().model_copy(update={"title": "Wrong schedule title", **mismatch}),
        )
        assert await uow.notification_outbox.list(principal(), limit=10) == []
        [audit] = await uow.process_events.list("notification.enqueue_failed")
        assert "Wrong schedule title" not in audit.model_dump_json()
