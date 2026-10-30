"""Owner-bound metadata and sealed integration keys (ADR-0270)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from agent_core.domain.hazards import contains_secret_material
from agent_core.domain.web import is_public_https_url

AuthScheme = Literal["bearer", "x-api-key"]


def secret_id(reference: str) -> UUID:
    """Accept only the canonical stored-reference namespace."""
    if not reference.startswith("stored:"):
        raise ValueError("Invalid integration key reference.")
    value = UUID(reference[7:])
    if reference != f"stored:{value}":
        raise ValueError("Invalid integration key reference.")
    return value


def validate_binding(label: str, origin: str, auth_scheme: str) -> None:
    """Reject non-canonical destinations and credential-shaped metadata."""
    if (
        not 1 <= len(label) <= 80
        or label != label.strip()
        or any(ord(c) < 32 or ord(c) == 127 for c in label)
        or contains_secret_material(label)
    ):
        raise ValueError("Choose a short label without credentials.")
    parsed = urlsplit(origin)
    if (
        not is_public_https_url(origin)
        or not origin.isascii()
        or origin.endswith(".")
        or origin != f"https://{parsed.hostname}"
    ):
        raise ValueError("Use an exact public HTTPS origin without a path or port.")
    if auth_scheme not in {"bearer", "x-api-key"}:
        raise ValueError("Unsupported integration key authentication type.")


def validate_value(value: str) -> None:
    if not 1 <= len(value) <= 4096 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError("Use a nonempty key of at most 4 KiB without whitespace.")


@dataclass(frozen=True, slots=True)
class IntegrationSecret:
    tenant_id: str
    principal_id: str
    id: UUID
    label: str
    origin: str
    auth_scheme: AuthScheme
    revision: int
    created_at: datetime
    updated_at: datetime
    sealed_value: str | None = field(repr=False)
    deleted_at: datetime | None = None

    def context(self) -> str:
        return json.dumps(
            [
                self.tenant_id,
                self.principal_id,
                str(self.id),
                self.origin,
                self.auth_scheme,
                self.revision,
            ],
            separators=(",", ":"),
        )

    def metadata(self) -> dict[str, object]:
        return {
            "id": str(self.id),
            "reference": f"stored:{self.id}",
            "label": self.label,
            "origin": self.origin,
            "auth_scheme": self.auth_scheme,
            "revision": self.revision,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
