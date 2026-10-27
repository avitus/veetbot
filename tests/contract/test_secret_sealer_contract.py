"""SecretSealer contract (ADR-0255): a sealed secret opens only in its own row and field."""

from __future__ import annotations

import base64

import pytest

from agent_core.adapters.mcp.sealing import AESGCMSecretSealer
from agent_core.ports.mcp import SecretSealer

KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")


def assert_secret_sealer_contract(sealer: SecretSealer, other_key: SecretSealer) -> None:
    sealed = sealer.seal("refresh-token", "local|magicpath|refresh_token")
    assert "refresh-token" not in sealed
    assert sealer.open(sealed, "local|magicpath|refresh_token").reveal() == "refresh-token"
    # A fresh nonce each time: the same secret never seals to the same text.
    assert sealer.seal("refresh-token", "local|magicpath|refresh_token") != sealed
    for context in ("local|magicpath|access_token", "other|magicpath|refresh_token"):
        with pytest.raises(PermissionError):
            sealer.open(sealed, context)
    tampered = sealed[:-2] + ("A" if sealed[-2] != "A" else "B") + sealed[-1]
    for value in (tampered, "v0." + sealed[3:], "garbage", ""):
        with pytest.raises(PermissionError):
            sealer.open(value, "local|magicpath|refresh_token")
    with pytest.raises(PermissionError):
        other_key.open(sealed, "local|magicpath|refresh_token")


def test_aes_gcm_secret_sealer_contract() -> None:
    assert_secret_sealer_contract(AESGCMSecretSealer.from_text(KEY), AESGCMSecretSealer(bytes(32)))
    with pytest.raises(ValueError, match="32 bytes"):
        AESGCMSecretSealer(b"short")
