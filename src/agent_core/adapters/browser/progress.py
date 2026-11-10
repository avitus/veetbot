"""Bounded decoding of optional hosted browser progress responses."""

from __future__ import annotations

import json
from typing import Any

import httpx

from agent_core.domain.browser import BrowserProviderError
from agent_core.domain.browser_diagnostics import MAX_PROGRESS_UPDATES, admit_browser_progress


async def read_progress_response(
    response: httpx.Response, *, maximum_body_bytes: int
) -> tuple[int, bytes]:
    """Decode bounded updates and exactly one terminal result without retrying the request."""
    pending = bytearray()
    total = 0
    records = 0
    result: tuple[int, bytes] | None = None
    maximum_stream_bytes = maximum_body_bytes + MAX_PROGRESS_UPDATES * 512 + 256
    try:
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > maximum_stream_bytes:
                raise ValueError("browser stream exceeds its bound")
            pending.extend(chunk)
            while (end := pending.find(b"\n")) >= 0:
                line = bytes(pending[:end])
                del pending[: end + 1]
                records += 1
                if result is not None or records > MAX_PROGRESS_UPDATES + 1:
                    raise ValueError("unexpected browser stream record")
                frame: Any = json.loads(line)
                if not isinstance(frame, dict):
                    raise ValueError("invalid browser stream record")
                if frame.get("type") == "progress":
                    if len(line) > 512 or records > MAX_PROGRESS_UPDATES:
                        raise ValueError("browser progress exceeds its bound")
                    # An invalid progress payload is discarded in its entirety.
                    if set(frame) == {"type", "progress"}:
                        admit_browser_progress(json.dumps(frame["progress"]))
                elif frame.get("type") == "result":
                    status = frame.get("status")
                    body = frame.get("body")
                    if (
                        set(frame) != {"type", "status", "body"}
                        or type(status) is not int
                        or not 200 <= status <= 599
                        or not isinstance(body, dict)
                    ):
                        raise ValueError("invalid browser stream result")
                    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
                    if len(encoded) > maximum_body_bytes:
                        raise ValueError("browser result exceeds its bound")
                    result = status, encoded
                else:
                    raise ValueError("unknown browser stream record")
            if len(pending) > maximum_body_bytes + 256:
                raise ValueError("browser stream record exceeds its bound")
        if pending or result is None:
            raise ValueError("browser stream ended without a complete result")
        return result
    except (ValueError, TypeError, RecursionError) as error:
        raise BrowserProviderError("tool.browser.output_invalid", retryable=False) from error
