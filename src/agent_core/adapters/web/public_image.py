"""Credential-free, DNS-pinned public image reads for ADR-0276."""

from __future__ import annotations

import asyncio
import io
import re
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx
from PIL import Image, ImageOps

from agent_core.adapters.jobs.http import resolve_job_host
from agent_core.domain.web import is_public_https_url
from agent_core.execution.egress_core import address_is_public


class _PhotoMetadata(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.images: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if (
            tag == "meta"
            and values.get("property") == "og:image"
            and (url := values.get("content"))
        ):
            self.images.add(url)


def _rendition(body: bytes) -> bytes:
    with Image.open(io.BytesIO(body), formats=("JPEG", "PNG", "WEBP")) as image:
        if image.width * image.height > 32_000_000:
            raise ValueError("Image exceeds the pixel limit.")
        image.thumbnail((1600, 1600))
        pixels = ImageOps.exif_transpose(image).convert("RGB")
        output = io.BytesIO()
        pixels.save(output, format="JPEG", quality=85)
        return output.getvalue()


class PublicImageReader:
    def __init__(
        self,
        resolver: Callable[[str], Awaitable[tuple[str, ...]]] = resolve_job_host,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.resolve = resolver
        self.transport = transport

    async def read(self, url: str) -> bytes:
        async with (
            asyncio.timeout(25),
            httpx.AsyncClient(
                transport=self.transport,
                trust_env=False,
                follow_redirects=False,
                limits=httpx.Limits(max_keepalive_connections=0),
            ) as client,
        ):
            selected = False
            for _ in range(6):
                if not is_public_https_url(url):
                    raise ValueError("Public HTTPS required.")
                target = httpx.URL(url)
                host = target.raw_host.decode("ascii")
                if not is_public_https_url(str(target)) or target.port not in (None, 443):
                    raise ValueError("Invalid URL.")
                addresses = await self.resolve(host)
                if not addresses or not all(address_is_public(ip) for ip in addresses):
                    raise ValueError("Public address required.")
                # Construct a fresh request so the client cookie jar cannot forward cookies.
                request = httpx.Request(
                    "GET",
                    target.copy_with(host=addresses[0]),
                    headers={"Host": host, "Accept-Encoding": "identity"},
                    extensions={"sni_hostname": host},
                )
                response = await client.send(request, stream=True)
                try:
                    if response.is_redirect and response.headers.get("location"):
                        url = urljoin(url, response.headers["location"])
                        continue
                    response.raise_for_status()
                    if response.headers.get("content-encoding", "identity") != "identity":
                        raise ValueError("Encoded response.")
                    html = response.headers.get("content-type", "").split(";")[0] == "text/html"
                    photo = re.search(r"/(i-[a-zA-Z0-9]+)(?:/|$)", target.path)
                    if html and (selected or not host.endswith(".smugmug.com") or photo is None):
                        raise ValueError("Share a selected photo or direct image URL.")
                    limit = 2_000_000 if html else 12_000_000
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > limit:
                            raise ValueError("Response too large.")
                        body.extend(chunk)
                    if not html:
                        return await asyncio.to_thread(_rendition, bytes(body))
                    metadata = _PhotoMetadata()
                    metadata.feed(body.decode("utf-8", errors="replace"))
                    if len(metadata.images) != 1:
                        raise ValueError("No unique selected photo.")
                    url = urljoin(url, metadata.images.pop())
                    assert photo is not None
                    if photo[1] not in httpx.URL(url).path.split("/"):
                        raise ValueError("Gallery metadata does not identify the selected photo.")
                    selected = True
                finally:
                    await response.aclose()
        raise ValueError("Too many redirects.")
