"""ADR-0128: the device ceremony mode and its secret-free page evidence."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_core.domain.browser import BrowserAuthenticationMode, BrowserPageEvidence


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
