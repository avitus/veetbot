"""A real stdio Calendar server with an in-process fake Google transport."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx

import calendar_mcp.__main__ as entry
from calendar_mcp.client import CalendarClient


def respond(request: httpx.Request) -> httpx.Response:
    if request.url.host == "oauth2.googleapis.com":
        return httpx.Response(200, json={"access_token": "secret-access", "expires_in": 3600})
    if "/calendarList/" in request.url.path:
        return httpx.Response(200, json={"id": "primary", "summary": "Calendar"})
    return httpx.Response(200, json={"items": []})


def client(raw: str) -> CalendarClient:
    return CalendarClient(
        raw, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))
    )


if __name__ == "__main__":
    with Path(sys.argv[1]).open("w") as stderr:
        sys.stderr = stderr
        entry.__dict__["CalendarClient"] = client
        entry.main([])
