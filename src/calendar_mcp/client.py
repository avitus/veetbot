"""Bounded Google Calendar reads with an operator-owned calendar selection."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

SCOPES = "https://www.googleapis.com/auth/calendar.calendarlist.readonly https://www.googleapis.com/auth/calendar.events.readonly"
API_ROOT = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
MAXIMUM_BYTES = 1024 * 1024
_EVENT_FIELDS = frozenset(
    {
        "id",
        "summary",
        "description",
        "location",
        "status",
        "htmlLink",
        "start",
        "end",
        "timeZone",
        "recurringEventId",
        "originalStartTime",
        "recurrence",
        "organizer",
        "attendees",
        "transparency",
        "visibility",
        "updated",
        "eventType",
        "hangoutLink",
    }
)
_INSTANT = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?(?:Z|[+-]\d\d:\d\d)$")


class CalendarError(RuntimeError):
    """A stable content-free provider failure."""

    def __init__(self, code: str) -> None:
        """Initialize the instance without exposing credentials in diagnostic output."""
        self.code = code
        super().__init__(code)


def text(value: object, maximum: int = 1024) -> str:
    """Validate a bounded nonblank string without control characters."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(c) < 32 for c in value)
    ):
        raise CalendarError("calendar.arguments_invalid")
    return value


def parse_credential(raw: str) -> dict[str, Any]:
    """Require the exact read-only scopes and a bounded distinct calendar selection."""
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {
            "client_id",
            "client_secret",
            "refresh_token",
            "scope",
            "calendar_ids",
        }:
            raise ValueError
        for name in ("client_id", "client_secret", "refresh_token"):
            text(value[name], 4096)
        if not isinstance(value["scope"], str) or set(value["scope"].split()) != set(
            SCOPES.split()
        ):
            raise ValueError
        ids = value["calendar_ids"]
        if not isinstance(ids, list) or not 1 <= len(ids) <= 16:
            raise ValueError
        for identifier in ids:
            text(identifier)
        if len(set(ids)) != len(ids):
            raise ValueError
    except (ValueError, TypeError, CalendarError) as exc:
        raise CalendarError("calendar.credential_rejected") from exc
    return value


class CalendarClient:
    def __init__(self, credential: str, *, http_client: httpx.AsyncClient | None = None) -> None:
        """Initialize the instance without exposing credentials in diagnostic output."""
        self._credential = parse_credential(credential)
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(30, connect=10), follow_redirects=False
        )
        self._owns_http = http_client is None
        self._access_token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        """Close the HTTP transport only when this adapter created it."""
        if self._owns_http:
            await self._http.aclose()

    async def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """Request fixed provider endpoints with bounded time and response size."""
        try:
            async with (
                asyncio.timeout(30),
                self._http.stream(method, url, follow_redirects=False, **kwargs) as response,
            ):
                status = response.status_code
                if status == 401 or (url == TOKEN_URL and status in {400, 403}):
                    raise CalendarError("calendar.credential_rejected")
                if status == 403:
                    raise CalendarError("calendar.access_denied")
                if status == 404:
                    raise CalendarError("calendar.not_found")
                if status == 429:
                    raise CalendarError("calendar.rate_limited")
                if status >= 500:
                    raise CalendarError("calendar.provider_unavailable")
                if status != 200:
                    raise CalendarError("calendar.provider_rejected")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    body.extend(chunk)
                    if len(body) > MAXIMUM_BYTES:
                        raise CalendarError("calendar.provider_output_invalid")
            result = json.loads(body)
            if not isinstance(result, dict):
                raise ValueError
            return result
        except (httpx.HTTPError, TimeoutError) as exc:
            raise CalendarError("calendar.provider_unavailable") from exc
        except (ValueError, UnicodeError) as exc:
            raise CalendarError("calendar.provider_output_invalid") from exc

    async def _token(self) -> str:
        """Refresh an expired access token under a lock and validate its lifetime."""
        async with self._lock:
            if self._access_token is not None and time.monotonic() < self._expires:
                return self._access_token
            result = await self._json(
                "POST",
                TOKEN_URL,
                data={
                    "client_id": self._credential["client_id"],
                    "client_secret": self._credential["client_secret"],
                    "refresh_token": self._credential["refresh_token"],
                    "grant_type": "refresh_token",
                },
            )
            token, expiry = result.get("access_token"), result.get("expires_in")
            if (
                not isinstance(token, str)
                or not token
                or len(token) > 8192
                or not isinstance(expiry, (int, float))
                or isinstance(expiry, bool)
                or not 0 < expiry <= 86400
                or not math.isfinite(expiry)
            ):
                raise CalendarError("calendar.provider_output_invalid")
            self._access_token = token
            self._expires = time.monotonic() + max(0, expiry - 60)
            return token

    async def _get(self, path: str, params: dict[str, str | int] | None = None) -> dict[str, Any]:
        """Retry a rejected access token once after refreshing the grant."""
        for attempt in range(2):
            token = await self._token()
            try:
                return await self._json(
                    "GET",
                    API_ROOT + path,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except CalendarError as exc:
                if exc.code != "calendar.credential_rejected" or attempt:
                    raise
                self._access_token = None
        raise AssertionError("Unreachable")

    def _calendar(self, calendar_id: str) -> str:
        """Enforce operator selection before encoding a Calendar API path segment."""
        text(calendar_id)
        if calendar_id not in self._credential["calendar_ids"]:
            raise CalendarError("calendar.not_selected")
        return quote(calendar_id, safe="")

    @staticmethod
    def _event(value: object) -> dict[str, Any]:
        """Validate event identity and time fields, then retain the supported fields."""
        if not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"]:
            raise CalendarError("calendar.provider_output_invalid")
        if value.get("status") != "cancelled":
            for field in ("start", "end"):
                point = value.get(field)
                if not isinstance(point, dict) or not any(
                    isinstance(point.get(key), str) and point[key] for key in ("date", "dateTime")
                ):
                    raise CalendarError("calendar.provider_output_invalid")
        return {key: item for key, item in value.items() if key in _EVENT_FIELDS}

    async def list_calendars(self) -> dict[str, Any]:
        """Read metadata only for calendars in the operator selection."""
        calendars = []
        for identifier in self._credential["calendar_ids"]:
            result = await self._get("/users/me/calendarList/" + self._calendar(identifier))
            if not isinstance(result.get("id"), str):
                raise CalendarError("calendar.provider_output_invalid")
            calendars.append(
                {
                    **{
                        key: result[key]
                        for key in ("summary", "timeZone", "accessRole", "primary")
                        if key in result
                    },
                    "id": identifier,
                }
            )
        return {"calendars": calendars, "complete": True}

    async def list_events(
        self,
        calendar_id: str,
        time_min: str,
        time_max: str,
        query: str | None = None,
        page_token: str | None = None,
        max_results: int = 50,
    ) -> dict[str, Any]:
        """Read one bounded page of expanded occurrences without altering time fields."""
        calendar = self._calendar(calendar_id)
        try:
            if not _INSTANT.fullmatch(text(time_min, 64)) or not _INSTANT.fullmatch(
                text(time_max, 64)
            ):
                raise ValueError
            lower, upper = datetime.fromisoformat(time_min), datetime.fromisoformat(time_max)
            if (
                lower.utcoffset() is None
                or upper.utcoffset() is None
                or not timedelta(0) < upper - lower <= timedelta(days=366)
            ):
                raise ValueError
            if type(max_results) is not int or not 1 <= max_results <= 100:
                raise ValueError
        except (ValueError, TypeError) as exc:
            raise CalendarError("calendar.arguments_invalid") from exc
        params: dict[str, str | int] = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "showDeleted": "false",
            "maxResults": max_results,
        }
        if query is not None:
            params["q"] = text(query, 512)
        if page_token is not None:
            params["pageToken"] = text(page_token, 4096)
        result = await self._get(f"/calendars/{calendar}/events", params)
        items, next_page = result.get("items", []), result.get("nextPageToken")
        if (
            not isinstance(items, list)
            or len(items) > max_results
            or (
                next_page is not None
                and (not isinstance(next_page, str) or not next_page or len(next_page) > 4096)
            )
        ):
            raise CalendarError("calendar.provider_output_invalid")
        return {
            "events": [self._event(item) for item in items],
            "next_page_token": next_page,
            "complete": next_page is None,
        }

    async def get_event(self, calendar_id: str, event_id: str) -> dict[str, Any]:
        """Read one event only after enforcing the selected-calendar boundary."""
        calendar = self._calendar(calendar_id)
        if event_id in {".", ".."}:
            raise CalendarError("calendar.arguments_invalid")
        event = quote(text(event_id), safe="")
        return {"event": self._event(await self._get(f"/calendars/{calendar}/events/{event}"))}
