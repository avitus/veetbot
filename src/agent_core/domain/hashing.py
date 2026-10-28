"""SHA-256 helpers shared by every module that names content by its digest."""

from __future__ import annotations

import hashlib
import json


def sha256_hex(data: bytes) -> str:
    """The lowercase hex SHA-256 of raw bytes."""

    return hashlib.sha256(data).hexdigest()


def canonical_json_sha256(payload: object) -> str:
    """The SHA-256 of a JSON-serialisable value in its canonical encoding.

    Keys are sorted and separators are minimal, so two equal values always
    hash alike regardless of how they were built.
    """

    return sha256_hex(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
