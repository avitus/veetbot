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
