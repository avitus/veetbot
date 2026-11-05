"""A bounded text file saved by an individually approved browser click (ADR-0294)."""

from __future__ import annotations

import base64
import re
from pathlib import PurePosixPath
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_BROWSER_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_BROWSER_DOWNLOAD_NAME_CHARACTERS = 96
_DEFAULT_NAME = "download.txt"
_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
}
_DEFAULT_MEDIA_TYPE = "text/plain"
# A PDF may be plain characters throughout, and readers accept its header anywhere
# in the first kilobyte.
_PDF_SIGNATURE = b"%PDF-"
_PDF_HEADER_BYTES = 1024

BrowserDownloadRefusal = Literal[
    "needs_approval",
    "origin_not_allowed",
    "unsupported_type",
    "too_large",
    "timed_out",
    "failed",
    "reader_unavailable",
]


def _clean_name(suggested: str) -> str:
    """A website's suggested name, reduced to a bare name of safe characters."""

    bare = PurePosixPath(suggested.replace("\\", "/")).name
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", bare)
    return cleaned[-MAX_BROWSER_DOWNLOAD_NAME_CHARACTERS:].lstrip("._") or _DEFAULT_NAME


def _media_type(name: str) -> str:
    """The type a clean name implies; a website's own claim is never read."""

    return _MEDIA_TYPES.get(PurePosixPath(name).suffix.lower(), _DEFAULT_MEDIA_TYPE)


def _text(data: bytes) -> str | None:
    """The file's text, or None when its bytes are not one of the two encodings."""

    if b"\0" in data or _PDF_SIGNATURE in data[:_PDF_HEADER_BYTES]:
        return None
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


class BrowserDownload(BaseModel):
    """What one approved click saved, or why it saved nothing.

    The bytes travel only from the isolated browser to the tool, which stores
    them as a capture; no event, log or ``browser.act`` result carries them. The name is
    website-supplied and stays ``EXTERNAL_UNTRUSTED`` with the rest of the result.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["saved", "refused"]
    reason: BrowserDownloadRefusal | None = None
    filename: str | None = Field(default=None, max_length=MAX_BROWSER_DOWNLOAD_NAME_CHARACTERS)
    media_type: str | None = Field(default=None, max_length=64)
    data_base64: str | None = Field(
        default=None, max_length=4 * ((MAX_BROWSER_DOWNLOAD_BYTES + 2) // 3), repr=False
    )

    @model_validator(mode="after")
    def carries_a_file_or_a_reason(self) -> Self:
        """Hold every saved file to the admission rules, wherever it was parsed.

        The worker parses what the browser service sent, so it repeats the
        service's checks rather than trusting a process that handles hostile pages.
        """

        if self.status == "saved":
            if self.reason is not None or self.filename is None or self.data_base64 is None:
                raise ValueError("a saved download names its file and no refusal")
            if self.filename != _clean_name(self.filename) or self.media_type != _media_type(
                self.filename
            ):
                raise ValueError("a saved download has a platform name and media type")
            data = base64.b64decode(self.data_base64, validate=True)
            data.decode("utf-8")
            if not data or len(data) > MAX_BROWSER_DOWNLOAD_BYTES or _text(data) is None:
                raise ValueError("a saved download is bounded text")
        if self.status == "refused" and (
            self.reason is None
            or self.filename is not None
            or self.media_type is not None
            or self.data_base64 is not None
        ):
            raise ValueError("a refused download carries only its reason")
        return self

    @classmethod
    def refused(cls, reason: BrowserDownloadRefusal) -> BrowserDownload:
        """A named refusal, which carries no file."""

        return cls(status="refused", reason=reason)

    @classmethod
    def from_bytes(cls, suggested_name: str, data: bytes) -> BrowserDownload:
        """Admit text within the limit; everything else becomes a named refusal."""

        if not data:
            return cls.refused("failed")
        if len(data) > MAX_BROWSER_DOWNLOAD_BYTES:
            return cls.refused("too_large")
        text = _text(data)
        if text is None:
            return cls.refused("unsupported_type")
        encoded = text.encode("utf-8")
        if len(encoded) > MAX_BROWSER_DOWNLOAD_BYTES:
            return cls.refused("too_large")
        name = _clean_name(suggested_name)
        return cls(
            status="saved",
            filename=name,
            media_type=_media_type(name),
            data_base64=base64.b64encode(encoded).decode("ascii"),
        )

    def data(self) -> bytes:
        """The file's UTF-8 bytes; a refusal has none."""

        if self.data_base64 is None:
            raise ValueError("a refused download has no bytes")
        return base64.b64decode(self.data_base64, validate=True)

    def text(self) -> str:
        """The file as text; a refusal has none."""

        return self.data().decode("utf-8")
