"""Versioned AES-GCM sealing with a caller-selected purpose namespace."""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_core.domain.credentials import SecretValue

_VERSION = "v1"
_NONCE_BYTES = 12


def _additional_data(context: str, purpose: str) -> bytes:
    # The tenant, server and field are bound in, so a sealed value cannot be
    # moved to another row or column and still open.
    return f"{purpose}.{_VERSION}|{context}".encode()


class AESGCMSecretSealer:
    def __init__(self, key: bytes, *, purpose: str = "veetbot.mcp") -> None:
        if len(key) != 32:
            raise ValueError("the sealing key must be 32 bytes")
        self._aead = AESGCM(key)
        self._purpose = purpose

    @classmethod
    def from_text(cls, encoded: str) -> AESGCMSecretSealer:
        """Decode the base64url ``MCP_AUTHORIZATION_KEY`` setting."""
        return cls(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))

    def seal(self, plaintext: str, context: str) -> str:
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aead.encrypt(
            nonce, plaintext.encode(), _additional_data(context, self._purpose)
        )
        encoded = base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii").rstrip("=")
        return f"{_VERSION}.{encoded}"

    def open(self, sealed: str, context: str) -> SecretValue:
        version, separator, encoded = sealed.partition(".")
        try:
            if version != _VERSION or not separator:
                raise ValueError
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            if len(raw) <= _NONCE_BYTES:
                raise ValueError
            plaintext = self._aead.decrypt(
                raw[:_NONCE_BYTES], raw[_NONCE_BYTES:], _additional_data(context, self._purpose)
            )
            return SecretValue(plaintext.decode())
        except (ValueError, binascii.Error, InvalidTag, UnicodeDecodeError) as exc:
            raise PermissionError("a sealed secret failed validation") from exc
