"""Bounded, provider-neutral media requests and reference images (ADR-0140)."""

from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

IMAGE_MAX_BYTES = 32 * 1024 * 1024
VIDEO_MAX_BYTES = 256 * 1024 * 1024
IMAGE_REFERENCE_MAX_BYTES = 20 * 1024 * 1024
IMAGE_REFERENCES_MAX_BYTES = 64 * 1024 * 1024
VIDEO_REFERENCE_MAX_BYTES = 10 * 1024 * 1024
ArtifactIdString = Annotated[
    str,
    Field(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"),
]


class ImageGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    prompt: str = Field(min_length=1, max_length=8000, pattern=r"\S")
    size: Literal[
        "1024x1024",
        "2048x1152",
        "1152x2048",
        "2496x1664",
        "1664x2496",
        "1504x2720",
        "2720x1504",
        "1824x2272",
        "2272x1824",
        "2048x2048",
        "1312x3136",
        "3136x1312",
    ] = "1024x1024"
    seed: int = Field(default=42, ge=0, le=2_147_483_647)


class VideoGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    prompt: str = Field(min_length=1, max_length=8000, pattern=r"\S")
    size: Literal["1280x768", "768x1280", "1920x1088", "1088x1920", "576x1024"] = "1280x768"
    duration_seconds: Literal[5, 10] = 5
    seed: int = Field(default=10, ge=0, le=2_147_483_647)


class ImageGenerationArguments(ImageGenerationRequest):
    reference_image_ids: list[ArtifactIdString] = Field(
        default_factory=list,
        max_length=8,
        description=(
            "Ordered PNG/JPEG/WebP artifact IDs from this chat. "
            "Refer to image 1, image 2, etc. in the prompt. "
            "Maximum 20 MiB each, 64 MiB total. Requires artifact.read."
        ),
    )


class VideoGenerationArguments(VideoGenerationRequest):
    reference_image_ids: list[ArtifactIdString] = Field(
        default_factory=list,
        max_length=2,
        description=(
            "PNG/JPEG/WebP artifact IDs from this chat: first frame, "
            "optionally followed by last frame. Maximum 10 MiB each. Requires artifact.read."
        ),
    )


@dataclass(frozen=True, slots=True)
class MediaImage:
    """Verified input bytes live only in memory, never in serialized tool arguments."""

    media_type: str
    data: bytes = field(repr=False)


class MediaInputError(Exception):
    def __init__(self, code: Literal["unavailable", "invalid", "too_large"]) -> None:
        self.code = code
        super().__init__(code)


def reference_limits(*, video: bool) -> tuple[int, int, int]:
    """Return maximum count, individual bytes and aggregate bytes."""
    if video:
        return 2, VIDEO_REFERENCE_MAX_BYTES, 2 * VIDEO_REFERENCE_MAX_BYTES
    return 8, IMAGE_REFERENCE_MAX_BYTES, IMAGE_REFERENCES_MAX_BYTES


def validate_media_images(images: tuple[MediaImage, ...], *, video: bool) -> None:
    count, per_image, total = reference_limits(video=video)
    if len(images) > count or sum(len(image.data) for image in images) > total:
        raise MediaInputError("too_large")
    for image in images:
        if len(image.data) > per_image:
            raise MediaInputError("too_large")
        signature = (
            "image/png"
            if image.data.startswith(b"\x89PNG\r\n\x1a\n")
            else "image/jpeg"
            if image.data.startswith(b"\xff\xd8\xff")
            else "image/webp"
            if image.data.startswith(b"RIFF") and image.data[8:12] == b"WEBP"
            else None
        )
        if signature is None or signature != image.media_type:
            raise MediaInputError("invalid")


class MediaGenerationError(Exception):
    """Only a fixed failure code may cross the provider boundary."""

    def __init__(
        self,
        code: Literal[
            "permission", "rejected", "timeout", "transport", "upstream", "invalid", "too_large"
        ],
    ) -> None:
        self.code = code
        super().__init__(code)
