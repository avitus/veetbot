"""Credential resolver fail-closed and redaction contract."""

import json

import pytest

from agent_core.domain.credentials import CredentialRef, SecretValue
from agent_core.ports.credentials import UnavailableCredentialResolver


async def test_sandbox_resolver_refuses_every_reference() -> None:
    with pytest.raises(PermissionError):
        await UnavailableCredentialResolver().resolve(CredentialRef("provider/openai"))


def test_secret_value_does_not_leak_or_serialize() -> None:
    secret = SecretValue("synthetic-secret-value")
    assert "synthetic-secret-value" not in str(secret)
    assert "synthetic-secret-value" not in repr(secret)
    with pytest.raises(TypeError):
        json.dumps(secret)
