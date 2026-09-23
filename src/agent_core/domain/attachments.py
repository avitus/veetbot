"""Boundary rules for a chat attachment upload (ADR-0118)."""

from __future__ import annotations

import re
import unicodedata

from agent_core.domain.errors import AttachmentValidationError

UPLOAD_NAME_MAX_BYTES = 255
UPLOAD_KEY_MAX_CHARS = 255
_MEDIA_TYPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")
_FORBIDDEN_NAME_CHARACTERS = frozenset({'"', "/", "\\"})
_FORBIDDEN_NAME_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def upload_name(name: str) -> str:
    """Accept a file name that is safe to quote in a Content-Disposition header.

    A name with a quote, a path separator, or a control character is refused at
    creation rather than escaped at read, as the artifact rules require.
    """

    stripped = name.strip()
    if not stripped or len(stripped.encode("utf-8")) > UPLOAD_NAME_MAX_BYTES:
        raise AttachmentValidationError("The file name is empty or longer than 255 bytes.")
    if any(
        character in _FORBIDDEN_NAME_CHARACTERS
        or unicodedata.category(character) in _FORBIDDEN_NAME_CATEGORIES
        for character in stripped
    ):
        raise AttachmentValidationError(
            "The file name may not contain quotes, path separators, or control characters."
        )
    return stripped


def upload_media_type(value: str) -> str:
    """Normalize a declared media type to `type/subtype`, refusing anything else."""

    media_type = value.split(";", 1)[0].strip().lower()
    if not _MEDIA_TYPE.fullmatch(media_type):
        raise AttachmentValidationError("The Content-Type is not a media type.")
    return media_type


def upload_key(value: str | None) -> str:
    if value is None or not value or len(value) > UPLOAD_KEY_MAX_CHARS:
        raise AttachmentValidationError(
            "An upload requires an Idempotency-Key of at most 255 characters."
        )
    return value
