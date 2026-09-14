"""Provider contract starts with dispatch confinement and truthful uncertainty."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from bland_mcp.client import BlandClient, BlandError

CALL_ID = "00000000-0000-0000-0000-000000000027"
NUMBER = "+14155550100"
RECIPIENT = "+14155550101"
KEY = "fixture-bland-credential"


def call_payload() -> dict[str, Any]:
    return {
        "phone_number": RECIPIENT,
        "task": "Ask whether the repair is ready.",
        "from": NUMBER,
        "max_duration": 5,
        "record": False,
        "tools": [],
        "transfer_list": {},
        "metadata": {"veetbot_request_id": CALL_ID},
    }


def provider_call() -> dict[str, Any]:
    return {
        "call_id": CALL_ID,
        "to": NUMBER,
        "from": RECIPIENT,
        "inbound": True,
        "completed": True,
        "queue_status": "complete",
        "created_at": datetime.now(UTC).isoformat(),
        "call_length": 1.5,
        "answered_by": "human",
        "summary": "Caller asked for a callback.",
        "concatenated_transcript": "user: Please call me back.",
        "error_message": "private diagnostic must never escape",
        "recording_url": "https://private.example.test/audio",
    }


async def test_bland_dispatch_contract() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "success", "call_id": CALL_ID})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BlandClient(KEY, NUMBER, http_client=http)
        result = await client.start_call(call_payload())
    assert result == {"provider_call_id": CALL_ID, "status": "accepted"}
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.bland.ai/v1/calls"
    assert requests[0].headers["authorization"] == KEY
    assert json.loads(requests[0].content) == call_payload()


@pytest.mark.parametrize("status", [302, 401, 429, 500])
async def test_bland_dispatched_failure_is_uncertain_and_never_retried(status: int) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"location": "https://attacker.example/"}, text=KEY)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    ) as http:
        client = BlandClient(KEY, NUMBER, http_client=http)
        with pytest.raises(BlandError) as error:
            await client.start_call(call_payload())
    assert error.value.code == "bland.outcome_unknown"
    assert error.value.uncertain
    assert len(requests) == 1
    assert KEY not in str(error.value)


async def test_bland_read_is_number_bound_and_normalized() -> None:
    payload = provider_call()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        client = BlandClient(KEY, NUMBER, http_client=http)
        result = await client.get_call(CALL_ID)
        assert result["provider_call_id"] == CALL_ID
        assert result["direction"] == "inbound"
        assert result["status"] == "completed"
        assert result["summary"] == payload["summary"]
        assert result["transcript"] == payload["concatenated_transcript"]
        assert "private" not in json.dumps(result)
        payload["to"] = "+14155550199"
        with pytest.raises(BlandError, match=r"bland\.call_not_found"):
            await client.get_call(CALL_ID)


async def test_bland_invalid_identifier_never_reaches_provider() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid call identifier reached the network")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BlandClient(KEY, NUMBER, http_client=http)
        with pytest.raises(BlandError, match=r"bland\.arguments_invalid"):
            await client.get_call("../../account")
    assert str(UUID(CALL_ID)) == CALL_ID


async def test_bland_provider_content_is_bounded_and_key_redacted() -> None:
    payload = {**provider_call(), "from": KEY, "summary": KEY, "concatenated_transcript": KEY}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        client = BlandClient(KEY, NUMBER, http_client=http)
        value = await client.get_call(CALL_ID)
        assert KEY not in json.dumps(value)
        payload["summary"] = "x" * 4091 + KEY
        value = await client.get_call(CALL_ID)
        assert value["summary"] == "x" * 4091 + "[reda"
        payload["summary"] = "x" * 5000
        payload["concatenated_transcript"] = "x" * 20000
        value = await client.get_call(CALL_ID)
        assert len(value["summary"]) == 4096 and value["summary_complete"] is False
        assert len(value["transcript"]) == 16384 and value["transcript_complete"] is False
        payload["concatenated_transcript"] = "x" * 1_048_576
        with pytest.raises(BlandError):
            await client.get_call(CALL_ID)
