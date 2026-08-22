"""Milestone 12 notification domain, payload, and deduplication gates."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from agent_core.domain.devices import (
    Device,
    DeviceKind,
    DeviceStatus,
    PushEnvironment,
    PushProvider,
    push_token_fingerprint,
)
from agent_core.domain.notifications import (
    DeliveryOutcome,
    Notification,
    NotificationDelivery,
    NotificationKind,
    NotificationPayload,
    NotificationSeverity,
    NotificationStatus,
    approval_requested_key,
    device_test_key,
    ops_alert_key,
    ops_recovered_key,
    question_asked_key,
    run_failed_key,
    schedule_occurrence_skipped_key,
    schedule_run_finished_key,
)
from agent_core.domain.runs import RunStatus
from agent_core.domain.schedules import OccurrenceDisposition

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "tests" / "corpora" / "notification_content"

DEVICE_ID = UUID("00000000-0000-0000-0000-000000001201")
NOTIFICATION_ID = UUID("00000000-0000-0000-0000-000000001202")
DELIVERY_ID = UUID("00000000-0000-0000-0000-000000001203")
SESSION_ID = UUID("00000000-0000-0000-0000-000000001204")
RUN_ID = UUID("00000000-0000-0000-0000-000000001205")
APPROVAL_ID = UUID("00000000-0000-0000-0000-000000001206")
QUESTION_ID = UUID("00000000-0000-0000-0000-000000001207")
SCHEDULE_ID = UUID("00000000-0000-0000-0000-000000001208")
OCCURRENCE_ID = UUID("00000000-0000-0000-0000-000000001209")
NOW = datetime(2026, 8, 22, 19, 30, tzinfo=UTC)


def _base_payload(kind: NotificationKind, title: str) -> dict[str, object]:
    return {
        "version": 1,
        "kind": kind,
        "title": title,
        "notification_id": NOTIFICATION_ID,
    }


def _approval_payload() -> dict[str, object]:
    return {
        **_base_payload(NotificationKind.APPROVAL_REQUESTED, "Approval needed"),
        "status": RunStatus.WAITING_FOR_APPROVAL,
        "tool_name": "calendar.create_event",
        "session_id": SESSION_ID,
        "run_id": RUN_ID,
        "approval_id": APPROVAL_ID,
    }


def test_notification_and_device_vocabularies_are_closed() -> None:
    assert {item.value for item in NotificationKind} == {
        "approval_requested",
        "question_asked",
        "run_failed",
        "schedule_run_finished",
        "schedule_occurrence_skipped",
        "ops_alert",
        "ops_recovered",
        "test",
    }
    assert {item.value for item in NotificationStatus} == {
        "pending",
        "dispatched",
        "superseded",
        "expired",
        "failed",
    }
    assert {item.value for item in DeliveryOutcome} == {
        "delivered",
        "retry",
        "unregistered",
        "rejected",
        "skipped",
    }
    assert {item.value for item in DeviceKind} == {
        "mobile",
        "laptop",
        "desktop",
        "web",
        "cli",
        "surface",
    }
    assert {item.value for item in PushProvider} == {"apns", "telegram"}


@pytest.mark.parametrize(
    "values",
    [
        _approval_payload(),
        {
            **_base_payload(NotificationKind.QUESTION_ASKED, "The agent has a question"),
            "status": RunStatus.WAITING_FOR_USER,
            "tool_name": "conversation.ask_user",
            "session_id": SESSION_ID,
            "run_id": RUN_ID,
            "question_id": QUESTION_ID,
        },
        {
            **_base_payload(NotificationKind.RUN_FAILED, "Run failed"),
            "status": RunStatus.FAILED,
            "session_id": SESSION_ID,
            "run_id": RUN_ID,
        },
        {
            **_base_payload(
                NotificationKind.SCHEDULE_RUN_FINISHED,
                "Scheduled run finished",
            ),
            "status": RunStatus.COMPLETED,
            "session_id": SESSION_ID,
            "run_id": RUN_ID,
            "schedule_id": SCHEDULE_ID,
            "occurrence_id": OCCURRENCE_ID,
        },
        {
            **_base_payload(
                NotificationKind.SCHEDULE_OCCURRENCE_SKIPPED,
                "Scheduled run skipped",
            ),
            "status": OccurrenceDisposition.SKIPPED_OVERLAP,
            "schedule_id": SCHEDULE_ID,
            "occurrence_id": OCCURRENCE_ID,
        },
        {
            **_base_payload(NotificationKind.OPS_ALERT, "Production alert"),
            "signal": "disk_free",
            "severity": NotificationSeverity.CRITICAL,
            "reason_code": "ops.disk_free",
            "release_id": "release-20260822",
        },
        {
            **_base_payload(NotificationKind.OPS_RECOVERED, "Production recovered"),
            "signal": "disk_free",
            "severity": NotificationSeverity.RECOVERED,
            "reason_code": "ops.disk_free_recovered",
        },
        _base_payload(NotificationKind.TEST, "Test notification"),
    ],
)
def test_closed_payload_accepts_only_kind_specific_vocabulary(values: dict[str, object]) -> None:
    payload = NotificationPayload.model_validate(values)
    dumped = payload.model_dump(mode="json", exclude_none=True)

    assert set(dumped) == set(values)
    assert dumped["kind"] == str(values["kind"])
    assert dumped["title"] == values["title"]
    if payload.kind in {
        NotificationKind.APPROVAL_REQUESTED,
        NotificationKind.QUESTION_ASKED,
        NotificationKind.RUN_FAILED,
        NotificationKind.SCHEDULE_RUN_FINISHED,
    }:
        assert isinstance(payload.status, RunStatus)
    elif payload.kind is NotificationKind.SCHEDULE_OCCURRENCE_SKIPPED:
        assert isinstance(payload.status, OccurrenceDisposition)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Approve transfer of the secret"),
        ("status", "almost_done"),
        ("tool_name", "not registry vocabulary"),
        ("signal", "disk_free"),
        ("severity", "critical"),
        ("reason_code", "ops.disk_free"),
        ("release_id", "release-20260822"),
    ],
)
def test_non_ops_payload_rejects_open_or_ops_only_values(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        NotificationPayload.model_validate({**_approval_payload(), field: value})


@pytest.mark.parametrize("kind", [NotificationKind.OPS_ALERT, NotificationKind.OPS_RECOVERED])
def test_ops_payload_requires_closed_ops_vocabulary(kind: NotificationKind) -> None:
    title = "Production alert" if kind is NotificationKind.OPS_ALERT else "Production recovered"
    with pytest.raises(ValidationError):
        NotificationPayload.model_validate(_base_payload(kind, title))


def test_notification_payload_is_content_free() -> None:
    members = sorted(CORPUS.glob("*.yaml"))
    assert len(members) >= 12
    assert set(NotificationPayload.model_fields) == {
        "version",
        "kind",
        "title",
        "status",
        "tool_name",
        "session_id",
        "run_id",
        "approval_id",
        "question_id",
        "schedule_id",
        "occurrence_id",
        "notification_id",
        "signal",
        "severity",
        "reason_code",
        "release_id",
    }

    for member in members:
        raw = yaml.safe_load(member.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        field = raw["field"]
        value = raw["value"]
        assert isinstance(field, str)
        with pytest.raises(ValidationError):
            NotificationPayload.model_validate({**_approval_payload(), field: value})

    serialized = json.dumps(
        NotificationPayload.model_validate(_approval_payload()).model_dump(mode="json")
    )
    assert all(
        str(yaml.safe_load(member.read_text())["value"]) not in serialized for member in members
    )

    for field, value in (
        ("signal", "disk is nearly full"),
        ("severity", "urgent"),
        ("reason_code", "database password was exposed"),
        ("release_id", "release id containing secret text"),
    ):
        values = {
            **_base_payload(NotificationKind.OPS_ALERT, "Production alert"),
            "signal": "disk_free",
            "severity": NotificationSeverity.CRITICAL,
            "reason_code": "ops.disk_free",
            field: value,
        }
        with pytest.raises(ValidationError):
            NotificationPayload.model_validate(values)


def test_bearer_token_corpus_member_is_explicitly_redacted() -> None:
    raw = yaml.safe_load((CORPUS / "11-bearer-token.yaml").read_text(encoding="utf-8"))

    assert raw == {
        "field": "bearer_token",
        "value": "<redacted bearer credential>",
    }


@given(
    approval_id=st.uuids(),
    run_id=st.uuids(),
    question_id=st.uuids(),
    occurrence_id=st.uuids(),
    device_id=st.uuids(),
    idempotency_key=st.text(min_size=1, max_size=64).filter(lambda value: bool(value.strip())),
)
def _check_generated_deduplication_keys(
    approval_id: UUID,
    run_id: UUID,
    question_id: UUID,
    occurrence_id: UUID,
    device_id: UUID,
    idempotency_key: str,
) -> None:
    keys = (
        approval_requested_key(approval_id),
        question_asked_key(run_id, question_id),
        run_failed_key(run_id),
        schedule_run_finished_key(occurrence_id),
        schedule_occurrence_skipped_key(occurrence_id),
        device_test_key(device_id, idempotency_key),
    )

    assert len(set(keys)) == len(keys)
    assert keys == (
        approval_requested_key(approval_id),
        question_asked_key(run_id, question_id),
        run_failed_key(run_id),
        schedule_run_finished_key(occurrence_id),
        schedule_occurrence_skipped_key(occurrence_id),
        device_test_key(device_id, idempotency_key),
    )


def test_notification_dedupe_keys_are_stable() -> None:
    assert approval_requested_key(APPROVAL_ID) == f"approval.requested:{APPROVAL_ID}"
    assert question_asked_key(RUN_ID, QUESTION_ID) == (
        f"run.waiting_for_user:{RUN_ID}:{QUESTION_ID}"
    )
    assert run_failed_key(RUN_ID) == f"run.failed:{RUN_ID}"
    assert schedule_run_finished_key(OCCURRENCE_ID) == (f"schedule.run_accounted:{OCCURRENCE_ID}")
    assert schedule_occurrence_skipped_key(OCCURRENCE_ID) == (
        f"schedule.occurrence.skipped:{OCCURRENCE_ID}"
    )
    assert device_test_key(DEVICE_ID, "retry-1") == f"device.test:{DEVICE_ID}:retry-1"
    assert ops_alert_key("tenant_a", "disk_free", 4) == "ops.tenant_a.disk_free.4"
    assert ops_recovered_key("tenant_a", "disk_free", 4) == ("ops.tenant_a.disk_free.4.recovered")
    assert ops_alert_key("tenant_a", "disk_free", 4) != ops_recovered_key(
        "tenant_a", "disk_free", 4
    )
    _check_generated_deduplication_keys()


def test_deduplication_components_are_bounded_and_unambiguous() -> None:
    assert ops_alert_key("tenant.example", "disk_free", 1) == ("ops.tenant.example.disk_free.1")
    invalid_calls: tuple[Callable[[], str], ...] = (
        lambda: device_test_key(DEVICE_ID, ""),
        lambda: device_test_key(DEVICE_ID, " " * 3),
        lambda: device_test_key(DEVICE_ID, "x" * 256),
        lambda: ops_alert_key("", "disk_free", 1),
        lambda: ops_alert_key("tenant_a", "disk.free", 1),
        lambda: ops_alert_key("tenant_a", "disk_free", 0),
    )
    for call in invalid_calls:
        with pytest.raises(ValueError):
            call()


def _device(**updates: object) -> Device:
    values: dict[str, object] = {
        "id": DEVICE_ID,
        "tenant_id": "tenant-a",
        "principal_id": "principal-a",
        "client_device_id": "ios-installation-1",
        "name": "Andy's iPhone",
        "kind": DeviceKind.MOBILE,
        "platform": "ios",
        "app_bundle_id": "com.veetbot.app",
        "push_provider": PushProvider.APNS,
        "push_token": "push-token-secret-value",
        "push_environment": PushEnvironment.SANDBOX,
        "push_token_updated_at": NOW,
        "muted_kinds": frozenset({NotificationKind.TEST}),
        "status": DeviceStatus.ACTIVE,
        "last_seen_at": NOW,
        "created_at": NOW - timedelta(minutes=1),
        "updated_at": NOW,
    }
    values.update(updates)
    return Device.model_validate(values)


def test_device_enforces_push_and_revocation_invariants_without_leaking_token() -> None:
    device = _device()
    dumped = json.dumps(device.model_dump(mode="json"))

    assert "push-token-secret-value" not in dumped
    assert push_token_fingerprint("push-token-secret-value") == "5fb0e1"

    invalid = (
        {"push_provider": None},
        {"push_token": ""},
        {"push_environment": None},
        {"push_provider": PushProvider.TELEGRAM},
        {"status": DeviceStatus.REVOKED, "revoked_at": NOW},
        {"status": DeviceStatus.REVOKED, "push_token": None, "push_provider": None},
    )
    for updates in invalid:
        with pytest.raises(ValidationError):
            _device(**updates)


def test_device_represents_tokenless_clients_revocation_and_future_surfaces() -> None:
    paired_chat_reference = "paired" + "-chat-reference"
    tokenless = _device(
        push_provider=None,
        push_token=None,
        push_environment=None,
        push_token_updated_at=None,
    )
    revoked = _device(
        push_provider=None,
        push_token=None,
        push_environment=None,
        status=DeviceStatus.REVOKED,
        revoked_at=NOW,
    )
    surface = _device(
        kind=DeviceKind.SURFACE,
        platform="telegram",
        app_bundle_id=None,
        push_provider=PushProvider.TELEGRAM,
        push_token=paired_chat_reference,
        push_environment=None,
    )

    assert tokenless.push_provider is None
    assert revoked.revoked_at == NOW
    assert surface.push_provider is PushProvider.TELEGRAM
    assert paired_chat_reference not in json.dumps(surface.model_dump(mode="json"))


def test_notification_and_delivery_records_enforce_internal_consistency() -> None:
    payload = NotificationPayload.model_validate(_approval_payload())
    notification = Notification(
        id=NOTIFICATION_ID,
        tenant_id="tenant-a",
        principal_id="principal-a",
        kind=NotificationKind.APPROVAL_REQUESTED,
        dedupe_key=approval_requested_key(APPROVAL_ID),
        session_id=SESSION_ID,
        run_id=RUN_ID,
        approval_id=APPROVAL_ID,
        payload=payload,
        priority=10,
        expires_at=NOW + timedelta(hours=1),
        status=NotificationStatus.PENDING,
        attempts=0,
        next_attempt_at=NOW,
        created_at=NOW,
    )
    delivery = NotificationDelivery(
        id=DELIVERY_ID,
        notification_id=NOTIFICATION_ID,
        device_id=DEVICE_ID,
        attempt=1,
        outcome=DeliveryOutcome.DELIVERED,
        provider_id="apns-id-1",
        attempted_at=NOW,
    )

    assert notification.payload.notification_id == notification.id
    assert delivery.attempt == 1

    with pytest.raises(ValidationError):
        Notification.model_validate(
            {**notification.model_dump(), "status": NotificationStatus.DISPATCHED}
        )
    with pytest.raises(ValidationError):
        NotificationDelivery.model_validate({**delivery.model_dump(), "attempt": 0})
