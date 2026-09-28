"""TensorScale's fixed-endpoint image and video adapter."""

import asyncio
import base64
from collections.abc import AsyncIterator

import httpx

from agent_core.domain.credentials import CredentialRef
from agent_core.domain.media import (
    IMAGE_MAX_BYTES,
    VIDEO_MAX_BYTES,
    ImageGenerationRequest,
    MediaGenerationError,
    MediaImage,
    VideoGenerationRequest,
    validate_media_images,
)
from agent_core.ports.credentials import CredentialResolver


class TensorScaleMediaProvider:
    def __init__(
        self, *, credentials: CredentialResolver, client: httpx.AsyncClient | None = None
    ) -> None:
        self._credentials = credentials
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)

    async def generate(
        self,
        request: ImageGenerationRequest | VideoGenerationRequest,
        *,
        timeout_seconds: float,
        images: tuple[MediaImage, ...] = (),
    ) -> AsyncIterator[bytes]:
        if timeout_seconds <= 0:
            raise MediaGenerationError("timeout")
        image = isinstance(request, ImageGenerationRequest)
        validate_media_images(images, video=not image)
        image_uris = [
            f"data:{item.media_type};base64,{base64.b64encode(item.data).decode('ascii')}"
            for item in images
        ]
        media_type = "image/png" if image else "video/mp4"
        maximum = IMAGE_MAX_BYTES if image else VIDEO_MAX_BYTES
        if isinstance(request, ImageGenerationRequest):
            endpoint = "https://api.tensorscale.io/v2/sensenova-u1.5/" + (
                "edit" if images else "t2i"
            )
            payload = request.model_dump()
            if images:
                payload["images"] = image_uris
        else:
            endpoint = "https://api.tensorscale.io/v2/ltx-2.5/fast"
            width, height = map(int, request.size.split("x"))
            payload = {
                "prompt": request.prompt,
                "width": width,
                "height": height,
                "num_frames": request.duration_seconds * 24 + 1,
                "frame_rate": 24,
                "seed": request.seed,
            }
            if images:
                payload["images"] = [
                    {
                        "data": uri,
                        "frame_idx": 0 if index == 0 else request.duration_seconds * 24,
                        "strength": 1.0,
                    }
                    for index, uri in enumerate(image_uris)
                ]
        try:
            async with asyncio.timeout(timeout_seconds):
                try:
                    key = await self._credentials.resolve(CredentialRef("tensorscale"))
                except PermissionError:
                    raise MediaGenerationError("permission") from None
                async with self._client.stream(
                    "POST",
                    endpoint,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {key.reveal()}",
                        "Accept": media_type,
                        "Accept-Encoding": "identity",
                    },
                    timeout=httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds)),
                    follow_redirects=False,
                ) as response:
                    if response.status_code in {401, 403}:
                        raise MediaGenerationError("permission")
                    if response.status_code in {400, 422}:
                        raise MediaGenerationError("rejected")
                    if response.status_code != 200:
                        raise MediaGenerationError("upstream")
                    if (
                        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                        != media_type
                    ):
                        raise MediaGenerationError("invalid")
                    # Identity transfer makes Content-Length an integrity check,
                    # and avoids compressed streams consuming unbounded memory.
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise MediaGenerationError("invalid")
                    declared = _declared_size(response, maximum)
                    total = 0
                    prefix = b""
                    validated = False
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        total += len(chunk)
                        if total > maximum:
                            raise MediaGenerationError("too_large")
                        if not validated:
                            prefix += chunk
                            if len(prefix) < 12:
                                continue
                            if not _has_signature(prefix, image=image):
                                raise MediaGenerationError("invalid")
                            validated = True
                            yield prefix
                            prefix = b""
                        else:
                            yield chunk
                    if not validated or (declared is not None and declared != total):
                        raise MediaGenerationError("invalid")
        except (TimeoutError, httpx.TimeoutException):
            raise MediaGenerationError("timeout") from None
        except httpx.HTTPError:
            raise MediaGenerationError("transport") from None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _declared_size(response: httpx.Response, maximum: int) -> int | None:
    raw = response.headers.get("content-length")
    if raw is None:
        return None
    try:
        size = int(raw)
    except ValueError:
        raise MediaGenerationError("invalid") from None
    if size <= 0:
        raise MediaGenerationError("invalid")
    if size > maximum:
        raise MediaGenerationError("too_large")
    return size


def _has_signature(prefix: bytes, *, image: bool) -> bool:
    if image:
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    return prefix[4:8] == b"ftyp" and int.from_bytes(prefix[:4], "big") >= 12
