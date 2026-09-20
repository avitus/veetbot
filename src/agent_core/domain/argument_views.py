"""The redacted view of tool arguments shown outside the executor.

The approval view, the approval digests, and the policy advisory layer all
describe arguments to someone other than the tool: a value under a sensitive
key or shaped like a credential is masked, a long string is cut, and a large
container is capped. The helpers live in the domain so the policy package can
use them without importing the executor, which already imports policy code.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, cast

REDACTED = "[REDACTED]"
TRUNCATED_SUFFIX = "…[TRUNCATED]"

SENSITIVE_ARGUMENT_KEY = re.compile(
    r"(?:api[_-]?key|secret|password|token|authorization|credential)", re.I
)
CREDENTIAL_SHAPE = re.compile(r"(?:api[_-]?key|secret|password|token|bearer)\s*[:=]\s*\S+", re.I)
# The longest string an approval view carries whole. A longer one is truncated and
# its full value is published as a digest instead, so exact client-side
# verification survives redaction.
APPROVAL_ARGUMENT_CEILING = 512


def approval_argument_value(value: Any, *, key: str | None = None) -> Any:
    if key is not None and SENSITIVE_ARGUMENT_KEY.search(key) is not None:
        return REDACTED
    if isinstance(value, str):
        if CREDENTIAL_SHAPE.search(value) is not None:
            return REDACTED
        if len(value) <= APPROVAL_ARGUMENT_CEILING:
            return value
        return f"{value[:APPROVAL_ARGUMENT_CEILING]}{TRUNCATED_SUFFIX}"
    if isinstance(value, dict):
        items = list(value.items())[:50]
        redacted = {
            str(nested_key): approval_argument_value(nested, key=str(nested_key))
            for nested_key, nested in items
        }
        if len(value) > len(items):
            redacted["[TRUNCATED]"] = f"{len(value) - len(items)} field(s) omitted"
        return redacted
    if isinstance(value, list):
        items = value[:50]
        redacted_items = [approval_argument_value(item) for item in items]
        if len(value) > len(items):
            redacted_items.append(f"[TRUNCATED: {len(value) - len(items)} item(s) omitted]")
        return redacted_items
    return value


def approval_argument_view(arguments: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], approval_argument_value(arguments))


def approval_argument_digests(arguments: dict[str, Any]) -> dict[str, str]:
    """Digest every top-level argument the view truncates for length.

    The view cuts a long string to its first 512 characters, so a client holding
    the original can no longer compare it for equality. The digest restores exact
    verification without publishing the value. A value redacted for sensitivity is
    never digested: the owner has no reason to verify a credential, and a digest of
    one is a brute-force target.
    """
    digests: dict[str, str] = {}
    for key, value in arguments.items():
        if not isinstance(value, str) or len(value) <= APPROVAL_ARGUMENT_CEILING:
            continue
        if SENSITIVE_ARGUMENT_KEY.search(key) is not None:
            continue
        if CREDENTIAL_SHAPE.search(value) is not None:
            continue
        digests[key] = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digests
