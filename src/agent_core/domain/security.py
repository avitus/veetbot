"""Shared content-only credential detection rules."""

from __future__ import annotations

import re

CREDENTIAL_VALUE_CHARS_PATTERN = r"[a-z0-9._~+/=-]"
CREDENTIAL_VALUE_BOUNDARY_PATTERN = r"(?=$|[^a-z0-9._~+/=-])"
OPAQUE_BEARER_PLACEHOLDER_PATTERN = (
    r"(?!(?:(?:your|example|sample)_)?(?:api_|access_)?token(?:_here)?\b)"
)
OPAQUE_BEARER_VALUE_PATTERN = (
    rf"{OPAQUE_BEARER_PLACEHOLDER_PATTERN}(?:"
    rf"[a-z0-9_-]{{16,}}\.[a-z0-9_-]{{6,}}\.[a-z0-9_-]{{6,}}|"
    rf"{CREDENTIAL_VALUE_CHARS_PATTERN}{{32,}}{CREDENTIAL_VALUE_BOUNDARY_PATTERN}|"
    rf"(?={CREDENTIAL_VALUE_CHARS_PATTERN}{{0,30}}[0-9_~+/=])"
    rf"{CREDENTIAL_VALUE_CHARS_PATTERN}{{8,31}}{CREDENTIAL_VALUE_BOUNDARY_PATTERN})"
)

SECRET_RULES: dict[str, re.Pattern[str]] = {
    "provider_key": re.compile(r"\b(?:sk-ant-|sk-)[A-Za-z0-9_-]{12,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "bearer_literal": re.compile(
        rf"(?:Authorization\s*:\s*Bearer\s+{CREDENTIAL_VALUE_CHARS_PATTERN}{{8,}}|"
        rf"\bBearer\s+{OPAQUE_BEARER_VALUE_PATTERN})",
        re.IGNORECASE,
    ),
    "dsn_password": re.compile(r"[a-z][a-z0-9+.-]*://[^\s:/]+:[^\s@/]+@", re.IGNORECASE),
    "assigned_secret": re.compile(
        r"(?i)\b(?:[A-Z0-9_]*(?:secret|token|password|api_?key)[A-Z0-9_]*)\s*=\s*"
        r"[\"'][^\"'\n]{13,}[\"']"
    ),
}

_LABELED_CREDENTIAL = re.compile(
    r"(?:api[_ -]?key|secret|password|token|authorization|credential|bearer)"
    r"\s*[:=]\s*\S+|\b(?:ghp|xox[baprs])[-_][A-Za-z0-9_-]{12,}",
    re.IGNORECASE,
)


def contains_credential(value: str) -> bool:
    """Return whether untrusted content resembles any governed secret family."""

    return _LABELED_CREDENTIAL.search(value) is not None or any(
        pattern.search(value) is not None for pattern in SECRET_RULES.values()
    )
