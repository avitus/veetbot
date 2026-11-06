"""Read a CSV response Chromium already received during an approved export.

No request is issued or replayed. Chromium bounds its own response buffer and
reports decoded byte counts before this observer asks for the one CSV body.
"""

import asyncio
import base64
from collections.abc import Callable
from email.message import Message
from typing import Any

from playwright.async_api import CDPSession
from playwright.async_api import Error as PlaywrightError

from agent_core.domain.browser import browser_origin
from agent_core.domain.browser_download import MAX_BROWSER_DOWNLOAD_BYTES, BrowserDownload


class CsvExportObserver:
    def __init__(
        self,
        session: CDPSession,
        frame_id: str,
        origin: str,
        reserve: Callable[[asyncio.Task[BrowserDownload]], bool],
    ) -> None:
        self.session = session
        self.frame_id = frame_id
        self.origin = origin
        self.reserve = reserve
        self.armed = False
        self.requests: set[str] = set()
        self.selected: str | None = None
        self.size = 0
        self.finished = asyncio.Event()
        self.failed = False

    def _own_url(self, url: str) -> bool:
        try:
            return browser_origin(url) == self.origin
        except ValueError:
            return False

    async def start(self) -> None:
        self.session.on("Network.requestWillBeSent", self.request)
        self.session.on("Network.responseReceived", self.response)
        self.session.on("Network.dataReceived", self.data)
        self.session.on("Network.loadingFinished", self.complete)
        self.session.on("Network.loadingFailed", self.failure)
        await self.session.send(
            "Network.enable",
            {
                "maxTotalBufferSize": MAX_BROWSER_DOWNLOAD_BYTES,
                "maxResourceBufferSize": MAX_BROWSER_DOWNLOAD_BYTES,
                "maxPostDataSize": 0,
            },
        )

    def request(self, event: dict[str, Any]) -> None:
        if not self.armed:
            return
        key = event["requestId"]
        if "redirectResponse" in event:
            self.requests.discard(key)
        elif (
            len(self.requests) < 32
            and event.get("frameId") == self.frame_id
            and event.get("type") in {"Fetch", "XHR"}
            and self._own_url(event["documentURL"])
            and self._own_url(event["request"]["url"])
        ):
            self.requests.add(key)

    def response(self, event: dict[str, Any]) -> None:
        key = event["requestId"]
        response = event["response"]
        if (
            key not in self.requests
            or self.selected is not None
            or event.get("frameId") != self.frame_id
            or response["status"] != 200
            or not self._own_url(response["url"])
        ):
            return
        disposition = Message()
        disposition["Content-Disposition"] = next(
            (
                str(value)
                for name, value in response.get("headers", {}).items()
                if name.lower() == "content-disposition"
            ),
            "",
        )
        filename = disposition.get_filename()
        attachment = disposition.get_content_disposition() == "attachment" and bool(
            filename and filename.casefold().endswith(".csv")
        )
        if (
            response.get("mimeType", "").lower() not in {"text/csv", "application/csv"}
            and not attachment
        ):
            return
        self.selected = key
        csv_name = filename if filename and filename.casefold().endswith(".csv") else "export.csv"
        task = asyncio.create_task(self.read(key, csv_name))
        if not self.reserve(task):
            task.cancel()

    def data(self, event: dict[str, Any]) -> None:
        if event["requestId"] == self.selected:
            self.size += event["dataLength"]
            if self.size > MAX_BROWSER_DOWNLOAD_BYTES:
                self.finished.set()

    def complete(self, event: dict[str, Any]) -> None:
        self.requests.discard(event["requestId"])
        if event["requestId"] == self.selected:
            self.finished.set()

    def failure(self, event: dict[str, Any]) -> None:
        self.requests.discard(event["requestId"])
        if event["requestId"] == self.selected:
            self.failed = True
            self.finished.set()

    async def read(self, key: str, filename: str) -> BrowserDownload:
        try:
            await self.finished.wait()
            if self.size > MAX_BROWSER_DOWNLOAD_BYTES:
                return BrowserDownload.refused("too_large")
            if self.failed or self.size < 1:
                return BrowserDownload.refused("failed")
            body = await self.session.send("Network.getResponseBody", {"requestId": key})
            raw = body["body"]
            data = (
                base64.b64decode(raw, validate=True)
                if body["base64Encoded"]
                else raw.encode("utf-8")
            )
            return BrowserDownload.from_bytes(filename, data)
        except (ValueError, PlaywrightError):
            return BrowserDownload.refused("failed")


def observe_csv_export(
    session: CDPSession,
    frame_id: str,
    origin: str,
    reserve: Callable[[asyncio.Task[BrowserDownload]], bool],
) -> CsvExportObserver:
    """The runtime's observer for one approved export act (ADR-0300 decision 2)."""
    return CsvExportObserver(session, frame_id, origin, reserve)
