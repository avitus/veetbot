"""Calendar's fake-provider contract runs without Google credentials or sockets."""

from __future__ import annotations

import importlib
import importlib.util
import json
from typing import Any

import httpx
import pytest

SCOPES = "https://www.googleapis.com/auth/calendar.calendarlist.readonly https://www.googleapis.com/auth/calendar.events.readonly"


def credential() -> dict[str, Any]:
    """Build a synthetic grant for selected-calendar contract tests."""
    return {
        "client_id": "client",
        "client_secret": "secret",
        "refresh_token": "refresh",
        "scope": SCOPES,
        "calendar_ids": ["primary"],
    }


def modules() -> tuple[Any, Any]:
    """Require the first-party adapter and server before exercising their contract."""
    assert importlib.util.find_spec("calendar_mcp") is not None, "Calendar adapter must exist"
    return importlib.import_module("calendar_mcp.client"), importlib.import_module(
        "calendar_mcp.server"
    )


@pytest.mark.asyncio
async def test_read_contract_and_pagination_preserve_calendar_time() -> None:
    """Verify read contract and pagination preserve calendar time."""
    module, server_module = modules()
    requests: list[httpx.Request] = []
    event = {
        "id": "meeting",
        "summary": "Meeting",
        "status": "confirmed",
        "start": {"dateTime": "2026-11-01T01:30:00-07:00", "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": "2026-11-01T01:30:00-08:00", "timeZone": "America/Los_Angeles"},
        "recurringEventId": "series",
        "originalStartTime": {"dateTime": "2026-11-01T01:30:00-07:00"},
        "htmlLink": "https://calendar.google.com/calendar/event?eid=meeting",
    }
    all_day = {
        "id": "day",
        "status": "confirmed",
        "start": {"date": "2026-11-02"},
        "end": {"date": "2026-11-03"},
    }

    def respond(request: httpx.Request) -> httpx.Response:
        """Supply deterministic Google responses through the injected mock transport."""
        requests.append(request)
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        assert request.method == "GET"
        if "/calendarList/" in request.url.path:
            return httpx.Response(
                200,
                json={"id": "primary", "summary": "Personal", "timeZone": "America/Los_Angeles"},
            )
        if request.url.path.endswith("/meeting"):
            return httpx.Response(200, json=event)
        assert request.url.params["singleEvents"] == "true"
        assert request.url.params["orderBy"] == "startTime"
        assert request.url.params["showDeleted"] == "false"
        return httpx.Response(200, json={"items": [event, all_day], "nextPageToken": "next"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = module.CalendarClient(json.dumps(credential()), http_client=http)
        server = server_module.create_server(client)
        assert {t.name for t in await server.list_tools()} == {
            "list_calendars",
            "list_events",
            "get_event",
        }
        result = await client.list_events(
            "primary", "2026-11-01T00:00:00-07:00", "2026-11-04T00:00:00-08:00"
        )
        assert result["events"] == [event, all_day]
        assert result["complete"] is False and result["next_page_token"] == "next"
        assert (await client.get_event("primary", "meeting"))["event"] == event
        assert (await client.list_calendars())["calendars"][0]["id"] == "primary"
        before = len(requests)
        for method, args in [
            (client.get_event, ("other", "meeting")),
            (client.list_events, ("other", "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z")),
        ]:
            with pytest.raises(module.CalendarError, match=r"calendar\.not_selected"):
                await method(*args)
        assert len(requests) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,code",
    [
        (401, "credential_rejected"),
        (403, "access_denied"),
        (404, "not_found"),
        (429, "rate_limited"),
        (500, "provider_unavailable"),
        (302, "provider_rejected"),
    ],
)
async def test_provider_errors_are_content_free(status: int, code: str) -> None:
    """Verify provider errors are content free."""
    module, _ = modules()

    def respond(request: httpx.Request) -> httpx.Response:
        """Supply deterministic Google responses through the injected mock transport."""
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        return httpx.Response(status, text="secret upstream content")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = module.CalendarClient(json.dumps(credential()), http_client=http)
        with pytest.raises(module.CalendarError, match=f"^calendar.{code}$"):
            await client.get_event("primary", "event")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-01-01", "2026-01-02"),
        ("2026-01-02T00:00:00Z", "2026-01-01T00:00:00Z"),
        ("2026-01-01T00:00:00Z", "2028-01-01T00:00:00Z"),
    ],
)
async def test_invalid_windows_never_request_google(start: str, end: str) -> None:
    """Verify invalid windows never request google."""
    module, _ = modules()

    def unexpected(request: httpx.Request) -> httpx.Response:
        """Fail if a rejected operation reaches its external boundary."""
        pytest.fail("Invalid arguments must fail before network access")

    async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected)) as http:
        client = module.CalendarClient(json.dumps(credential()), http_client=http)
        with pytest.raises(module.CalendarError, match="arguments_invalid"):
            await client.list_events("primary", start, end)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [[], {"items": "invalid"}, {"items": [{"id": "broken"}]}, {"items": [], "nextPageToken": 7}],
)
async def test_invalid_provider_output_is_not_an_empty_agenda(payload: Any) -> None:
    """Verify invalid provider output is not an empty agenda."""
    module, _ = modules()

    def respond(request: httpx.Request) -> httpx.Response:
        """Supply deterministic Google responses through the injected mock transport."""
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = module.CalendarClient(json.dumps(credential()), http_client=http)
        with pytest.raises(module.CalendarError, match="provider_output_invalid"):
            await client.list_events("primary", "2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z")


@pytest.mark.asyncio
async def test_refresh_retry_is_bounded_and_revocation_is_terminal() -> None:
    """Verify refresh retry is bounded and revocation is terminal."""
    module, _ = modules()
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        """Supply deterministic Google responses through the injected mock transport."""
        calls.append(str(request.url.host))
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        return httpx.Response(401, text="Do not expose this")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = module.CalendarClient(json.dumps(credential()), http_client=http)
        with pytest.raises(module.CalendarError, match="credential_rejected"):
            await client.get_event("primary", "event")
    assert calls == ["oauth2.googleapis.com", "www.googleapis.com"] * 2


@pytest.mark.parametrize(
    "change",
    [
        {"scope": "https://www.googleapis.com/auth/calendar"},
        {"calendar_ids": []},
        {"calendar_ids": ["primary", "primary"]},
        {"calendar_ids": [1]},
        {"refresh_token": ""},
    ],
)
def test_credentials_cannot_expand_google_permissions(change: dict[str, Any]) -> None:
    """Verify credentials cannot expand google permissions."""
    module, _ = modules()
    with pytest.raises(module.CalendarError, match="credential_rejected"):
        module.CalendarClient(json.dumps({**credential(), **change}))


@pytest.mark.asyncio
async def test_oversized_provider_response_fails_closed() -> None:
    """Verify oversized provider response fails closed."""
    module, _ = modules()

    def respond(request: httpx.Request) -> httpx.Response:
        """Supply deterministic Google responses through the injected mock transport."""
        if request.url.host == "oauth2.googleapis.com":
            return httpx.Response(200, json={"access_token": "access", "expires_in": 3600})
        return httpx.Response(200, content=b"x" * (module.MAXIMUM_BYTES + 1))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client = module.CalendarClient(json.dumps(credential()), http_client=http)
        with pytest.raises(module.CalendarError, match="provider_output_invalid"):
            await client.get_event("primary", "event")
