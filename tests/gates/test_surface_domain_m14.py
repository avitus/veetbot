"""Milestone 14 surface-domain gates from inbound-surfaces.md."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_core.domain import surfaces
from agent_core.domain.sessions import SessionStatus
from agent_core.policy.scopes import PLATFORM_SCOPES

NOW = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)
SURFACE_ID = UUID("00000000-0000-4000-8000-000000000140")
SESSION_ID = UUID("00000000-0000-4000-8000-000000000141")
PAIRING_ID = UUID("00000000-0000-4000-8000-000000000142")


def test_surface_scopes_extend_the_closed_platform_vocabulary() -> None:
    assert {"surface.read", "surface.write"} <= PLATFORM_SCOPES


def test_pairing_code_is_secret_hashed_single_presentation_and_bound() -> None:
    issued = surfaces.issue_pairing_code(
        pairing_id=PAIRING_ID,
        surface_id=SURFACE_ID,
        tenant_id="tenant-a",
        principal_id="owner",
        created_by_principal_id="owner",
        granted_scopes=frozenset({"run.read", "run.write"}),
        label="Owner phone",
        now=NOW,
        expires_after=timedelta(minutes=10),
        max_attempts=5,
        code="B7zF4nQ2",
        salt=b"0123456789abcdef",
    )

    assert issued.code.get_secret_value() == "B7zF4nQ2"
    assert issued.record.code_hash != b"B7zF4nQ2"
    assert issued.record.code_salt == b"0123456789abcdef"
    assert issued.record.expires_at == NOW + timedelta(minutes=10)
    assert issued.record.granted_scopes == frozenset({"run.read", "run.write"})
    assert surfaces.pairing_code_matches(issued.record, "B7zF4nQ2") is True
    assert surfaces.pairing_code_matches(issued.record, "wrong") is False
    assert "B7zF4nQ2" not in repr(issued.record)


def test_pairing_code_refuses_invalid_lifetime_and_attempt_state() -> None:
    issued = surfaces.issue_pairing_code(
        pairing_id=PAIRING_ID,
        surface_id=SURFACE_ID,
        tenant_id="tenant-a",
        principal_id="owner",
        created_by_principal_id="owner",
        granted_scopes=frozenset(),
        label=None,
        now=NOW,
        expires_after=timedelta(minutes=10),
        max_attempts=5,
        code="B7zF4nQ2",
        salt=b"0123456789abcdef",
    )

    with pytest.raises(ValidationError, match="attempts cannot exceed max_attempts"):
        issued.record.model_copy(update={"attempts": 6}).__class__.model_validate(
            {**issued.record.model_dump(), "attempts": 6}
        )
    with pytest.raises(ValueError, match="positive"):
        surfaces.issue_pairing_code(
            pairing_id=PAIRING_ID,
            surface_id=SURFACE_ID,
            tenant_id="tenant-a",
            principal_id="owner",
            created_by_principal_id="owner",
            granted_scopes=frozenset(),
            label=None,
            now=NOW,
            expires_after=timedelta(0),
            max_attempts=5,
            code="B7zF4nQ2",
            salt=b"0123456789abcdef",
        )


@pytest.mark.parametrize("chat_ref", ["12345", "447700900123", "wa-user_12"])
def test_direct_message_external_key_is_stable_and_bounded(chat_ref: str) -> None:
    assert surfaces.direct_message_key(chat_ref) == f"dm:{chat_ref}"


@pytest.mark.parametrize("chat_ref", ["", "  ", "group:12", "thread:12", "x" * 253])
def test_direct_message_external_key_rejects_reserved_or_oversized_shapes(
    chat_ref: str,
) -> None:
    with pytest.raises(ValueError):
        surfaces.direct_message_key(chat_ref)


def test_surface_session_rotation_rules_are_closed_and_never_reuse_a_mapping() -> None:
    mapping = surfaces.SurfaceSession(
        id=PAIRING_ID,
        surface_id=SURFACE_ID,
        tenant_id="tenant-a",
        principal_id="owner",
        external_key="dm:12345",
        session_id=SESSION_ID,
        created_at=NOW,
    )

    assert (
        surfaces.rotation_reason(
            mapping,
            now=NOW + timedelta(hours=1),
            idle_after=timedelta(hours=24),
            session_status=SessionStatus.ACTIVE,
            last_message_at=NOW,
            mapped_agent_version="agent@1",
            current_agent_version="agent@1",
        )
        is None
    )
    assert (
        surfaces.rotation_reason(
            mapping,
            now=NOW + timedelta(hours=25),
            idle_after=timedelta(hours=24),
            session_status=SessionStatus.ACTIVE,
            last_message_at=NOW,
            mapped_agent_version="agent@1",
            current_agent_version="agent@1",
        )
        is surfaces.SessionRotationReason.IDLE
    )
    assert (
        surfaces.rotation_reason(
            mapping,
            now=NOW,
            idle_after=timedelta(hours=24),
            session_status=SessionStatus.CLOSED,
            last_message_at=NOW,
            mapped_agent_version="agent@1",
            current_agent_version="agent@1",
        )
        is surfaces.SessionRotationReason.SESSION_CLOSED
    )
    assert (
        surfaces.rotation_reason(
            mapping,
            now=NOW,
            idle_after=timedelta(hours=24),
            session_status=SessionStatus.ACTIVE,
            last_message_at=NOW,
            mapped_agent_version="agent@1",
            current_agent_version="agent@2",
        )
        is surfaces.SessionRotationReason.AGENT_VERSION_CHANGED
    )
    assert mapping.rotated_at is None
    rotated = surfaces.rotate_surface_session(mapping, NOW + timedelta(minutes=1))
    assert rotated.rotated_at == NOW + timedelta(minutes=1)
    with pytest.raises(ValueError, match="already rotated"):
        surfaces.rotate_surface_session(rotated, NOW + timedelta(minutes=2))


def test_surface_receipts_are_content_free_and_string_keyed() -> None:
    receipt = surfaces.InboundReceipt(
        surface_id=SURFACE_ID,
        external_update_id="wamid.HBgMNTU1",
        received_at=NOW,
        disposition=surfaces.InboundDisposition.REJECTED_UNPAIRED,
        reason_code="surface.unpaired",
    )

    assert set(receipt.model_dump()) == {
        "surface_id",
        "external_update_id",
        "received_at",
        "disposition",
        "session_id",
        "run_id",
        "reason_code",
    }


def test_reply_chunking_preserves_text_and_channel_bound() -> None:
    text = "a" * 4080 + "\n\n" + "b" * 40 + "\n" + "c" * 4097

    chunks = surfaces.chunk_surface_text(text, limit=4096)

    assert all(0 < len(chunk) <= 4096 for chunk in chunks)
    assert "".join(chunks) == text


@pytest.mark.parametrize(
    "text",
    [
        # A paragraph break that starts on the last character a chunk may hold.
        "a" * 4095 + "\n\n" + "b" * 10,
        # A line break exactly one past the last character a chunk may hold.
        "a" * 4096 + "\n" + "b" * 10,
    ],
    ids=["paragraph_break_at_limit_minus_one", "line_break_at_limit"],
)
def test_reply_chunking_never_exceeds_the_limit_at_a_separator_boundary(text: str) -> None:
    chunks = surfaces.chunk_surface_text(text, limit=4096)

    assert all(0 < len(chunk) <= 4096 for chunk in chunks)
    assert "".join(chunks) == text


def _surface_limits(**overrides: object) -> surfaces.SurfaceLimits:
    values: dict[str, object] = {
        "poll_timeout_seconds": 30,
        "session_idle_seconds": 86400,
        "code_expiry_seconds": 600,
        "code_max_attempts": 5,
        "lockout_seconds": 3600,
        "per_sender_messages_per_minute": 20,
        "max_active_runs_per_tenant": 4,
        "daily_cost": 25,
        "monthly_cost": 250,
        "max_cost_per_run": 10,
        "synthesis_reserve_cost": 2,
        "inbound_text_max_chars": 32768,
        "chunk_size": 4096,
        "claim_batch": 100,
        "lease_seconds": 30,
        "fallback_poll_seconds": 2,
        "retry_delays_seconds": [30, 120],
        "reply_max_attempts": 8,
    }
    values.update(overrides)
    return surfaces.SurfaceLimits.model_validate(values)


def test_surface_run_budget_is_finite_and_fits_the_daily_ceiling() -> None:
    from decimal import Decimal

    limits = _surface_limits()

    assert limits.run_budget == surfaces.SurfaceRunBudget(
        max_cost=Decimal("10"), synthesis_reserve_cost=Decimal("2")
    )
    with pytest.raises(ValidationError, match="reserve"):
        _surface_limits(synthesis_reserve_cost=10)
    with pytest.raises(ValidationError, match="daily"):
        _surface_limits(max_cost_per_run=26)
    with pytest.raises(ValidationError):
        _surface_limits(max_cost_per_run=0)
    with pytest.raises(ValidationError):
        _surface_limits(reply_max_attempts=0)


def test_surface_run_budget_caps_an_uncapped_run_and_keeps_a_lower_cap() -> None:
    from decimal import Decimal

    from agent_core.domain.runs import RunLimits

    budget = surfaces.SurfaceRunBudget(max_cost=Decimal("10"), synthesis_reserve_cost=Decimal("2"))
    uncapped = RunLimits(max_steps=32, max_model_calls=24, max_tool_calls=64)
    lower = uncapped.model_copy(
        update={"max_cost": Decimal("3"), "synthesis_reserve_cost": Decimal("1")}
    )
    higher = RunLimits(
        max_steps=160,
        max_model_calls=120,
        max_tool_calls=160,
        max_cost=Decimal("30"),
        synthesis_reserve_cost=Decimal("3"),
    )

    capped = budget.applied_to(uncapped)
    assert capped.max_cost == Decimal("10")
    assert capped.synthesis_reserve_cost == Decimal("2")
    assert capped.max_steps == 32
    assert budget.applied_to(lower) == lower
    assert budget.applied_to(higher).max_cost == Decimal("10")
    assert budget.applied_to(higher).synthesis_reserve_cost == Decimal("2")


def test_a_revoked_surface_cannot_acquire_a_delivery_route() -> None:
    from agent_core.domain.devices import Device, DeviceKind, DeviceStatus, PushProvider

    revoked = Device(
        id=SURFACE_ID,
        tenant_id="tenant-a",
        principal_id="owner",
        client_device_id="telegram:configured",
        name="Veetbot Telegram",
        kind=DeviceKind.SURFACE,
        platform=PushProvider.TELEGRAM.value,
        muted_kinds=frozenset(),
        capabilities=frozenset(),
        status=DeviceStatus.REVOKED,
        revoked_at=NOW,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )

    with pytest.raises(ValueError, match="active"):
        revoked.with_surface_route(PushProvider.TELEGRAM, "12345", NOW)
