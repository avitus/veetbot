"""ADR-0128: the device ceremony mode, its secret-free page evidence, and the
rule that grants wait for a sign-in's recorded outcome."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from agent_core.domain.browser import (
    BrowserAuthenticationMode,
    BrowserAuthenticationRecord,
    BrowserAuthenticationStatus,
    BrowserPageEvidence,
    sign_in_outcome_unrecorded,
)
from tests.contract.support import NOW


def test_device_mode_and_redacted_page_evidence() -> None:
    assert {mode.value for mode in BrowserAuthenticationMode} == {"remote", "device"}
    assert BrowserAuthenticationMode("device") is BrowserAuthenticationMode.DEVICE

    evidence = BrowserPageEvidence(
        on_allowed_origin=True, path="/secret-sentinel", challenge_visible=False
    )

    assert evidence.path == "/secret-sentinel"
    assert "secret-sentinel" not in repr(evidence)
    assert "secret-sentinel" not in str(evidence)


def test_page_evidence_is_frozen_and_bounded() -> None:
    evidence = BrowserPageEvidence(on_allowed_origin=False, path="/", challenge_visible=True)

    with pytest.raises(ValidationError):
        evidence.path = "/learn"
    with pytest.raises(ValidationError):
        BrowserPageEvidence(on_allowed_origin=True, path="/" + "a" * 4096, challenge_visible=False)


def _record(minute: int, status: BrowserAuthenticationStatus) -> BrowserAuthenticationRecord:
    created = NOW + timedelta(minutes=minute)
    return BrowserAuthenticationRecord(
        id=UUID(int=0xA000 + minute),
        tenant_id="tenant",
        principal_id="owner",
        profile_id=UUID(int=0xA0),
        status=status,
        expires_at=created + timedelta(minutes=5),
        created_at=created,
        updated_at=created,
    )


Status = BrowserAuthenticationStatus


@pytest.mark.parametrize(
    ("statuses", "unrecorded"),
    [
        ((), False),
        ((Status.READY,), False),
        ((Status.EXPIRED,), False),
        ((Status.CANCELLED,), False),
        ((Status.AUTHENTICATION_REQUIRED,), True),
        ((Status.NEEDS_USER,), True),
        # A later begin was admitted, so the older ceremony had already ended.
        ((Status.AUTHENTICATION_REQUIRED, Status.CANCELLED), False),
        ((Status.READY, Status.AUTHENTICATION_REQUIRED), True),
    ],
)
def test_only_the_newest_sign_in_can_lack_a_recorded_outcome(
    statuses: tuple[BrowserAuthenticationStatus, ...], unrecorded: bool
) -> None:
    """ADR-0128 D10: grants wait for the newest ceremony's outcome, in any order."""

    records = [_record(minute, status) for minute, status in enumerate(statuses)]

    assert sign_in_outcome_unrecorded(records) is unrecorded
    assert sign_in_outcome_unrecorded(list(reversed(records))) is unrecorded


def test_every_record_at_the_newest_instant_counts() -> None:
    ready = _record(0, Status.READY)
    open_ceremony = _record(0, Status.AUTHENTICATION_REQUIRED).model_copy(
        update={"id": UUID(int=0xB000)}
    )

    assert sign_in_outcome_unrecorded([ready, open_ceremony])
    assert sign_in_outcome_unrecorded([open_ceremony, ready])
