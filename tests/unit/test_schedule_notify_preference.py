"""ADR-0225 part D: a schedule may notify only when something needs the owner.

`notify` is a definition field with a default that matches today's behaviour.
With `attention_only`, a completed run raises no `schedule_run_finished`
notification; failures, cancellations, approvals, questions and skipped
occurrences are never suppressed by the schedule's preference.
"""

import hashlib
import json
from uuid import UUID

import pytest

from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.application.notification_producer import NotificationProducer
from agent_core.application.schedule_service import (
    _definition_from_revision,
    _definition_hash,
    _revision,
)
from agent_core.domain.notifications import NotificationKind
from agent_core.domain.runs import RunStatus
from agent_core.domain.schedules import (
    OccurrenceDisposition,
    ScheduleDefinition,
    ScheduleNotify,
    ScheduleOccurrence,
    ScheduleRevision,
)
from agent_core.scheduling.accounting import ScheduleOutcomeAccountant
from tests.contract.support import NOW, RUN_ID, SESSION_ID, memory_uow_factory, principal, run
from tests.contract.test_schedule_repository_contract import revision, schedule


async def _notifications_after(status: RunStatus, notify: ScheduleNotify) -> list[NotificationKind]:
    clock, factory = await memory_uow_factory()
    ids = SequenceIdFactory()
    initial = schedule()
    pinned = revision().model_copy(update={"notify": notify})
    occurrence = ScheduleOccurrence(
        id=UUID(int=930),
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
        await uow.runs.create(run(status=status))
        await uow.schedule_occurrences.insert(occurrence)
    accountant = ScheduleOutcomeAccountant(
        uow_factory=factory,
        clock=clock,
        ids=ids,
        notification_producer=NotificationProducer(clock=clock, ids=ids),
    )
    assert await accountant.account(RUN_ID)
    async with factory() as uow:
        notifications = await uow.notification_outbox.list(principal(), limit=10)
    return [item.payload.kind for item in notifications]


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED])
async def test_every_report_keeps_todays_notification(status: RunStatus) -> None:
    kinds = await _notifications_after(status, ScheduleNotify.EVERY_REPORT)
    assert kinds == [NotificationKind.SCHEDULE_RUN_FINISHED]


async def test_attention_only_suppresses_a_completed_report() -> None:
    assert await _notifications_after(RunStatus.COMPLETED, ScheduleNotify.ATTENTION_ONLY) == []


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.CANCELLED])
async def test_attention_only_still_reports_a_run_that_did_not_complete(status: RunStatus) -> None:
    kinds = await _notifications_after(status, ScheduleNotify.ATTENTION_ONLY)
    assert kinds == [NotificationKind.SCHEDULE_RUN_FINISHED]


def test_a_stored_revision_without_the_field_reads_as_every_report() -> None:
    stored = revision().model_dump(mode="json")
    stored.pop("notify", None)
    assert ScheduleRevision.model_validate(stored).notify is ScheduleNotify.EVERY_REPORT


def test_the_preference_survives_a_conversational_update() -> None:
    pinned = revision().model_copy(update={"notify": ScheduleNotify.ATTENTION_ONLY})
    definition = _definition_from_revision(pinned)
    assert definition.notify is ScheduleNotify.ATTENTION_ONLY
    rebuilt = _revision(schedule(), definition, principal(), NOW)
    assert rebuilt.notify is ScheduleNotify.ATTENTION_ONLY


def test_the_default_leaves_an_existing_idempotency_hash_unchanged() -> None:
    definition = _definition_from_revision(revision())
    # A request from before ADR-0150, ADR-0225 and ADR-0221 had none of these.
    legacy = definition.model_dump(
        mode="json", exclude={"browser_profile_id", "notify", "call_plan"}
    )
    previous = hashlib.sha256(
        json.dumps(legacy, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    ).hexdigest()
    assert _definition_hash(definition) == previous
    changed = definition.model_copy(update={"notify": ScheduleNotify.ATTENTION_ONLY})
    assert _definition_hash(changed) != previous


def test_the_preference_is_a_closed_set() -> None:
    values = _definition_from_revision(revision()).model_dump(mode="json")
    values["notify"] = "never"
    with pytest.raises(ValueError):
        ScheduleDefinition.model_validate(values)


def test_a_stored_revision_omits_the_default_so_an_older_release_reads_it() -> None:
    """A revision is stored as before unless the owner chose a preference.

    A release from before ADR-0225 part D forbids unknown keys, so writing the
    default `notify` would make every new revision unreadable after a rollback.
    """
    from agent_core.adapters.persistence.mappers import schedule_revision_values

    default = schedule_revision_values(revision())["definition"]
    assert "notify" not in default
    chosen = revision().model_copy(update={"notify": ScheduleNotify.ATTENTION_ONLY})
    assert schedule_revision_values(chosen)["definition"]["notify"] == "attention_only"
