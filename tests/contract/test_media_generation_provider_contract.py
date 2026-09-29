"""Shared media-provider contract, initially bound to TensorScale's HTTP adapter."""

import asyncio
import base64
import json
from collections.abc import AsyncIterator, Callable
from typing import Literal, cast

import httpx
import pytest

from agent_core.adapters.credentials import MappingCredentialResolver
from agent_core.adapters.media.tensorscale import TensorScaleMediaProvider
from agent_core.domain.media import (
    ImageGenerationRequest,
    MediaGenerationError,
    MediaImage,
    MediaInputError,
    VideoGenerationRequest,
)
from agent_core.ports.media import MediaGenerationProvider

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-image"
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"synthetic-video"
ProviderFactory = Callable[[httpx.AsyncClient], MediaGenerationProvider]


def tensorscale(client: httpx.AsyncClient) -> MediaGenerationProvider:
    return TensorScaleMediaProvider(
        credentials=MappingCredentialResolver({"tensorscale": "synthetic-media-credential"}),
        client=client,
    )


@pytest.fixture(params=[tensorscale])
def factory(request: pytest.FixtureRequest) -> ProviderFactory:
    return cast(ProviderFactory, request.param)


@pytest.mark.parametrize("video", [False, True])
async def test_generation_stream_contract(factory: ProviderFactory, video: bool) -> None:
    data = MP4 if video else PNG
    media_type = "video/mp4" if video else "image/png"
    seen: list[httpx.Request] = []

    async def wire(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=data, headers={"content-type": media_type})

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        provider = factory(client)
        request = (
            VideoGenerationRequest(prompt="A lighthouse")
            if video
            else ImageGenerationRequest(prompt="A lighthouse")
        )
        result = b"".join([part async for part in provider.generate(request, timeout_seconds=60)])
        await provider.close()

    assert result == data
    assert len(seen) == 1
    assert seen[0].url == (
        "https://api.tensorscale.io/v2/ltx-2.5/fast"
        if video
        else "https://api.tensorscale.io/v2/sensenova-u1.5/t2i"
    )
    assert seen[0].headers["authorization"] == "Bearer synthetic-media-credential"
    assert seen[0].headers["accept"] == media_type
    assert json.loads(seen[0].content) == (
        {
            "prompt": "A lighthouse",
            "width": 1280,
            "height": 768,
            "num_frames": 121,
            "frame_rate": 24,
            "seed": 10,
        }
        if video
        else {"prompt": "A lighthouse", "size": "1024x1024", "seed": 42}
    )


@pytest.mark.parametrize("video", [False, True])
@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("duration", [5, 10])
async def test_reference_images_translate_to_edit_or_frame_conditioning(
    factory: ProviderFactory, video: bool, count: int, duration: Literal[5, 10]
) -> None:
    seen: list[httpx.Request] = []
    images = (MediaImage("image/png", PNG), MediaImage("image/jpeg", b"\xff\xd8\xffsynthetic"))[
        :count
    ]

    async def wire(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            content=MP4 if video else PNG,
            headers={"content-type": "video/mp4" if video else "image/png"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        request = (
            VideoGenerationRequest(prompt="Use these images", duration_seconds=duration)
            if video
            else ImageGenerationRequest(prompt="Use these images")
        )
        _ = [
            part
            async for part in factory(client).generate(request, timeout_seconds=30, images=images)
        ]
    [sent] = seen
    assert sent.url.path == ("/v2/ltx-2.5/fast" if video else "/v2/sensenova-u1.5/edit")
    uris = [
        f"data:{item.media_type};base64,{base64.b64encode(item.data).decode('ascii')}"
        for item in images
    ]
    assert json.loads(sent.content)["images"] == (
        [
            {"data": uri, "frame_idx": 0 if index == 0 else duration * 24, "strength": 1.0}
            for index, uri in enumerate(uris)
        ]
        if video
        else uris
    )


@pytest.mark.parametrize("video", [False, True])
@pytest.mark.parametrize("kind", ["signature", "mime", "count", "size", "empty"])
async def test_invalid_reference_images_never_make_a_paid_request(
    factory: ProviderFactory, video: bool, kind: str
) -> None:
    images = {
        "signature": (MediaImage("image/png", b"not an image"),),
        "mime": (MediaImage("image/jpeg", PNG),),
        "count": (MediaImage("image/png", PNG),) * (3 if video else 9),
        "size": (MediaImage("image/png", PNG + b"x" * ((10 if video else 20) * 1024 * 1024)),),
        "empty": (MediaImage("image/png", b""),),
    }[kind]
    seen: list[httpx.Request] = []

    def wire(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            content=MP4 if video else PNG,
            headers={"content-type": "video/mp4" if video else "image/png"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(MediaInputError):
            _ = [
                part
                async for part in factory(client).generate(
                    VideoGenerationRequest(prompt="x")
                    if video
                    else ImageGenerationRequest(prompt="x"),
                    timeout_seconds=30,
                    images=images,
                )
            ]
    assert seen == []


class ChunkedMedia(httpx.AsyncByteStream):
    def __init__(self, *, oversized: bool = False, wait: bool = False) -> None:
        self.oversized = oversized
        self.wait = wait
        self.started = asyncio.Event()
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        self.started.set()
        yield PNG[:3]
        yield PNG[3:]
        if self.wait:
            await asyncio.Event().wait()
        if self.oversized:
            for _ in range(513):
                yield b"x" * 65536

    async def aclose(self) -> None:
        self.closed = True


async def test_streaming_limit_does_not_depend_on_content_length(factory: ProviderFactory) -> None:
    stream = ChunkedMedia(oversized=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "image/png"}, stream=stream
            )
        )
    ) as client:
        with pytest.raises(MediaGenerationError) as caught:
            async for _ in factory(client).generate(
                ImageGenerationRequest(prompt="Lighthouse"), timeout_seconds=10
            ):
                pass
    assert caught.value.code == "too_large"
    assert stream.closed


async def test_provider_cancellation_closes_the_http_response(factory: ProviderFactory) -> None:
    stream = ChunkedMedia(wait=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "image/png"}, stream=stream
            )
        )
    ) as client:

        async def consume() -> None:
            async for _ in factory(client).generate(
                ImageGenerationRequest(prompt="Lighthouse"), timeout_seconds=10
            ):
                pass

        task = asyncio.create_task(consume())
        await asyncio.wait_for(stream.started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert stream.closed


async def test_missing_credentials_never_send_a_request() -> None:
    seen: list[httpx.Request] = []

    async def wire(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=PNG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        provider = TensorScaleMediaProvider(
            credentials=MappingCredentialResolver({}), client=client
        )
        with pytest.raises(MediaGenerationError, match="permission"):
            _ = [
                part
                async for part in provider.generate(
                    ImageGenerationRequest(prompt="Lighthouse"), timeout_seconds=10
                )
            ]
    assert seen == []


async def test_nondefault_video_options_translate_without_provider_specific_arguments(
    factory: ProviderFactory,
) -> None:
    seen: list[httpx.Request] = []

    async def wire(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=MP4, headers={"content-type": "video/mp4"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        request = VideoGenerationRequest(
            prompt="Lighthouse", duration_seconds=10, size="768x1280", seed=123
        )
        _ = [part async for part in factory(client).generate(request, timeout_seconds=60)]
    assert json.loads(seen[0].content) == {
        "prompt": "Lighthouse",
        "width": 768,
        "height": 1280,
        "num_frames": 241,
        "frame_rate": 24,
        "seed": 123,
    }


async def test_close_releases_the_owned_http_pool() -> None:
    provider = TensorScaleMediaProvider(credentials=MappingCredentialResolver({}))
    assert not provider._client.is_closed
    await provider.close()
    assert provider._client.is_closed


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (302, "upstream"),
        (400, "rejected"),
        (401, "permission"),
        (403, "permission"),
        (429, "upstream"),
        (500, "upstream"),
    ],
)
async def test_errors_never_retry_follow_redirects_or_expose_bodies(
    factory: ProviderFactory, status: int, code: str
) -> None:
    seen: list[httpx.Request] = []

    async def wire(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            status,
            text="synthetic-media-credential private prompt",
            headers={"location": "https://attacker.example/steal"},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(wire), follow_redirects=True
    ) as client:
        with pytest.raises(MediaGenerationError) as caught:
            _ = [
                part
                async for part in factory(client).generate(
                    ImageGenerationRequest(prompt="private prompt"), timeout_seconds=60
                )
            ]
    assert caught.value.code == code
    assert str(caught.value) == code
    assert len(seen) == 1


@pytest.mark.parametrize(
    ("content", "headers", "code"),
    [
        (b"", {"content-type": "image/png"}, "invalid"),
        (b"<html>error</html>", {"content-type": "image/png"}, "invalid"),
        (PNG, {"content-type": "text/html"}, "invalid"),
        (PNG, {"content-type": "image/png", "content-length": "999"}, "invalid"),
        (PNG, {"content-type": "image/png", "content-length": str(33 * 1024 * 1024)}, "too_large"),
    ],
)
async def test_response_validation_contract(
    factory: ProviderFactory, content: bytes, headers: dict[str, str], code: str
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=content, headers=headers)
        )
    ) as client:
        with pytest.raises(MediaGenerationError) as caught:
            _ = [
                part
                async for part in factory(client).generate(
                    ImageGenerationRequest(prompt="A lighthouse"), timeout_seconds=60
                )
            ]
    assert caught.value.code == code


@pytest.mark.parametrize("timeout", [False, True])
async def test_transport_failures_are_safe_and_not_retried(
    factory: ProviderFactory, timeout: bool
) -> None:
    seen: list[httpx.Request] = []

    async def wire(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        error = httpx.ReadTimeout if timeout else httpx.ConnectError
        raise error("synthetic-media-credential private prompt")

    async with httpx.AsyncClient(transport=httpx.MockTransport(wire)) as client:
        with pytest.raises(MediaGenerationError) as caught:
            _ = [
                part
                async for part in factory(client).generate(
                    ImageGenerationRequest(prompt="private prompt"), timeout_seconds=60
                )
            ]
    assert caught.value.code == ("timeout" if timeout else "transport")
    assert "private" not in str(caught.value)
    assert len(seen) == 1
