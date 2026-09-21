"""Fixed-origin Bland REST transport; provider failures never carry provider text."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

API_ROOT = "https://api.bland.ai/v1"
MAXIMUM_BODY_BYTES = 1_048_576
_PHONE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_DISPATCH_KEYS = frozenset(
    {
        "phone_number",
        "task",
        "from",
        "max_duration",
        "record",
        "tools",
        "transfer_list",
        "metadata",
        "voice",
        "webhook",
        "voicemail",
        "wait_for_greeting",
    }
)
_VOICEMAIL_MESSAGE_CHARACTERS = 1000


class BlandError(Exception):
    def __init__(self, code: str, *, uncertain: bool = False) -> None:
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


def identifier(value: object) -> str:
    if not isinstance(value, str):
        raise BlandError("bland.arguments_invalid")
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise BlandError("bland.arguments_invalid") from exc


def _text(value: object, limit: int, *, credential: str) -> tuple[str, bool]:
    if value is None:
        return "", False
    if not isinstance(value, str):
        raise BlandError("bland.provider_output_invalid")
    raw = value.replace(credential, "[redacted]").encode("utf-8")
    return raw[:limit].decode("utf-8", errors="ignore"), len(raw) <= limit


def _instant(value: object) -> str:
    if not isinstance(value, str):
        raise BlandError("bland.provider_output_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("missing offset")
        return parsed.astimezone(UTC).isoformat()
    except ValueError as exc:
        raise BlandError("bland.provider_output_invalid") from exc


def _approved_voicemail(value: object) -> bool:
    """Hang up, or leave the owner-approved message; SMS variants stay deferred."""
    if value == {"action": "hangup"}:
        return True
    if not isinstance(value, dict) or set(value) != {"action", "message"}:
        return False
    message = value["message"]
    return (
        value["action"] == "leave_message"
        and isinstance(message, str)
        and 1 <= len(message) <= _VOICEMAIL_MESSAGE_CHARACTERS
    )


class BlandClient:
    def __init__(
        self, key: str, number: str, *, http_client: httpx.AsyncClient | None = None
    ) -> None:
        if not key or any(char.isspace() for char in key) or not _PHONE.fullmatch(number):
            raise BlandError("bland.configuration_invalid")
        self._key = key
        self.number = number
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=5), follow_redirects=False, trust_env=False
        )
        self._owns_http = http_client is None

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, str | int] | None = None,
    ) -> dict[str, Any]:
        mutating = method != "GET"
        try:
            async with self._http.stream(
                method,
                API_ROOT + path,
                headers={"authorization": self._key},
                json=payload,
                params=params,
                follow_redirects=False,
            ) as response:
                if not 200 <= response.status_code < 300:
                    if mutating:
                        raise BlandError("bland.outcome_unknown", uncertain=True)
                    code = (
                        "bland.credential_rejected"
                        if response.status_code in {401, 403}
                        else "bland.rate_limited"
                        if response.status_code == 429
                        else "bland.provider_unavailable"
                        if response.status_code >= 500
                        else "bland.provider_rejected"
                    )
                    raise BlandError(code)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > MAXIMUM_BODY_BYTES:
                        raise BlandError(
                            "bland.outcome_unknown"
                            if mutating
                            else "bland.provider_output_invalid",
                            uncertain=mutating,
                        )
                    body.extend(chunk)
                result: object = json.loads(body)
                if not isinstance(result, dict):
                    raise ValueError("not an object")
                return result
        except (httpx.HTTPError, UnicodeError, ValueError) as exc:
            raise BlandError(
                "bland.outcome_unknown" if mutating else "bland.provider_unavailable",
                uncertain=mutating,
            ) from exc

    async def start_call(self, payload: dict[str, Any]) -> dict[str, Any]:
        recipient = payload.get("phone_number")
        task = payload.get("task")
        duration = payload.get("max_duration")
        metadata = payload.get("metadata")
        if (
            set(payload) - _DISPATCH_KEYS
            or not isinstance(recipient, str)
            or _PHONE.fullmatch(recipient) is None
            or not isinstance(task, str)
            or not 1 <= len(task.encode()) <= 12_000
            or payload.get("from") != self.number
            or type(duration) is not int
            or not 1 <= duration <= 5
            or payload.get("record") is not False
            or payload.get("tools") != []
            or payload.get("transfer_list") != {}
            or not isinstance(metadata, dict)
            or set(metadata) != {"veetbot_request_id"}
            or payload.get("wait_for_greeting") is not True
            or not _approved_voicemail(payload.get("voicemail"))
        ):
            raise BlandError("bland.arguments_invalid")
        identifier(metadata["veetbot_request_id"])
        result = await self._request("POST", "/calls", payload=payload)
        try:
            call_id = identifier(result.get("call_id"))
            if result.get("status") != "success":
                raise BlandError("bland.provider_output_invalid")
        except BlandError as exc:
            raise BlandError("bland.outcome_unknown", uncertain=True) from exc
        return {"provider_call_id": call_id, "status": "accepted"}

    async def get_call(self, call_id: str) -> dict[str, Any]:
        normalized_id = identifier(call_id)
        value = await self._request("GET", "/calls/" + normalized_id)
        try:
            if identifier(value.get("call_id")) != normalized_id:
                raise BlandError("bland.provider_output_invalid")
            inbound = value.get("inbound")
            completed = value.get("completed")
            if type(inbound) is not bool or type(completed) is not bool:
                raise BlandError("bland.provider_output_invalid")
            if value.get("to" if inbound else "from") != self.number:
                raise BlandError("bland.call_not_found")
            counterparty = value.get("from" if inbound else "to")
            if not isinstance(counterparty, str) or len(counterparty) > 64:
                raise BlandError("bland.provider_output_invalid")
            duration = value.get("call_length", 0)
            if type(duration) not in {int, float} or not math.isfinite(duration) or duration < 0:
                raise BlandError("bland.provider_output_invalid")
            summary, summary_complete = _text(value.get("summary"), 4096, credential=self._key)
            transcript, transcript_complete = _text(
                value.get("concatenated_transcript"), 16_384, credential=self._key
            )
            raw_status = value.get("queue_status")
            answered = value.get("answered_by")
            status = (
                "busy"
                if raw_status == "busy"
                else "no_answer"
                if raw_status in {"no-answer", "no_answer"}
                else "failed"
                if raw_status in {"failed", "error", "canceled", "cancelled"}
                else "voicemail"
                if completed and answered in {"voicemail", "machine"}
                else "completed"
                if completed
                else "active"
            )
            metadata = value.get("metadata")
            request_id = metadata.get("veetbot_request_id") if isinstance(metadata, dict) else None
            if request_id is not None:
                request_id = identifier(request_id)
            return {
                "provider_call_id": normalized_id,
                "request_id": request_id,
                "number": self.number,
                "counterparty": counterparty.replace(self._key, "[redacted]"),
                "direction": "inbound" if inbound else "outbound",
                "status": status,
                "created_at": _instant(value.get("created_at")),
                "duration_minutes": duration,
                "summary": summary,
                "summary_complete": summary_complete,
                "transcript": transcript,
                "transcript_complete": transcript_complete,
            }
        except (TypeError, ValueError) as exc:
            raise BlandError("bland.provider_output_invalid") from exc

    async def list_call_ids(
        self, *, inbound: bool, start_date: str, offset: int = 0, limit: int = 25
    ) -> dict[str, Any]:
        if type(inbound) is not bool or type(offset) is not int or not 0 <= offset <= 1_000_000:
            raise BlandError("bland.arguments_invalid")
        if type(limit) is not int or not 1 <= limit <= 25:
            raise BlandError("bland.arguments_invalid")
        since = _instant(start_date)
        value = await self._request(
            "GET",
            "/calls",
            params={
                "inbound": str(inbound).lower(),
                "to_number" if inbound else "from_number": self.number,
                "start_date": since,
                "from": offset,
                "limit": limit,
                "ascending": "true",
            },
        )
        calls = value.get("calls")
        if not isinstance(calls, list) or len(calls) > limit:
            raise BlandError("bland.provider_output_invalid")
        try:
            ids = [identifier(call.get("call_id")) for call in calls if isinstance(call, dict)]
        except BlandError as exc:
            raise BlandError("bland.provider_output_invalid") from exc
        if len(ids) != len(calls):
            raise BlandError("bland.provider_output_invalid")
        return {
            "provider_call_ids": ids,
            "next_offset": offset + len(ids) if len(ids) == limit else None,
        }

    async def stop_call(self, call_id: str) -> dict[str, Any]:
        call_id = identifier(call_id)
        existing = await self.get_call(call_id)
        if existing["status"] != "active":
            return {"provider_call_id": call_id, "stopped": True}
        result = await self._request("POST", "/calls/" + call_id + "/stop")
        if result.get("status") != "success":
            raise BlandError("bland.outcome_unknown", uncertain=True)
        return {"provider_call_id": call_id, "stopped": True}
