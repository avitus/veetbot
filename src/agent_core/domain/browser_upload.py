"""Bounded, path-free image transfer for an individually approved browser upload."""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from agent_core.domain.browser import BrowserAction, BrowserActionKind
from agent_core.domain.media import (
    ArtifactIdString,
    MediaImage,
    MediaInputError,
    validate_media_images,
)

MAX_BROWSER_IMAGE_BYTES = 5 * 1024 * 1024
MAX_BROWSER_UPLOAD_BODY_BYTES = 7 * 1024 * 1024
_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class BrowserUploadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    image_id: ArtifactIdString
    expected_revision: str = Field(min_length=1, max_length=128)
    ref: str = Field(min_length=1, max_length=128)

    def action(self) -> BrowserAction:
        return BrowserAction(
            kind=BrowserActionKind.CLICK, expected_revision=self.expected_revision, ref=self.ref
        )


@dataclass(frozen=True, slots=True)
class BrowserImageFile:
    filename: str
    image: MediaImage

    def __post_init__(self) -> None:
        if len(self.image.data) > MAX_BROWSER_IMAGE_BYTES:
            raise MediaInputError("too_large")
        validate_media_images((self.image,), video=False)
        extension = _EXTENSIONS[self.image.media_type]
        if not re.fullmatch(r"[0-9a-f-]{36}\." + extension, self.filename):
            raise MediaInputError("invalid")

    @classmethod
    def for_artifact(cls, artifact_id: str, image: MediaImage) -> BrowserImageFile:
        extension = _EXTENSIONS.get(image.media_type)
        if extension is None:
            raise MediaInputError("invalid")
        return cls(f"{artifact_id.lower()}.{extension}", image)


class BrowserImagePayload(BaseModel):
    """Service-only wire envelope; never a model tool argument or result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    filename: str = Field(max_length=41)
    media_type: str = Field(max_length=32)
    data_base64: str = Field(max_length=4 * ((MAX_BROWSER_IMAGE_BYTES + 2) // 3), repr=False)

    @classmethod
    def encode(cls, image: BrowserImageFile) -> BrowserImagePayload:
        return cls(
            filename=image.filename,
            media_type=image.image.media_type,
            data_base64=base64.b64encode(image.image.data).decode("ascii"),
        )

    def decode(self) -> BrowserImageFile:
        try:
            data = base64.b64decode(self.data_base64, validate=True)
        except (binascii.Error, ValueError):
            raise MediaInputError("invalid") from None
        return BrowserImageFile(self.filename, MediaImage(self.media_type, data))
