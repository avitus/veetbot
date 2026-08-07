"""Credential references and an accidentally-safe resolved secret wrapper."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Never


@dataclass(frozen=True, slots=True)
class CredentialRef:
    name: str


class SecretValue:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SecretValue('[REDACTED]')"

    def __str__(self) -> str:
        return "[REDACTED]"

    def __reduce__(self) -> Never:
        raise TypeError("SecretValue cannot be serialized")
