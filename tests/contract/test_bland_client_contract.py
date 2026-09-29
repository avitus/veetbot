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
        "voicemail": {"action": "hangup"},
        "wait_for_greeting": True,
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


@pytest.mark.parametrize(
    "change",
    [
        {"voicemail": {"action": "leave_message_and_sms", "message": "x", "sms": {"message": "x"}}},
        {"voicemail": {"action": "leave_message", "message": "x", "sms": {"message": "x"}}},
        {"voicemail": {"action": "leave_message"}},
        {"voicemail": {"action": "leave_message", "message": ""}},
        {"voicemail": {"action": "leave_message", "message": "x" * 1001}},
        {"voicemail": {"action": "ignore"}},
        {"voicemail": {"action": "hangup", "message": "x"}},
        {"voicemail": None},
        {"wait_for_greeting": False},
        {"wait_for_greeting": None},
    ],
)
async def test_bland_dispatch_requires_explicit_voicemail_and_greeting_order(
    change: dict[str, Any],
) -> None:
    """ADR-0108: leave the approved message or hang up; never SMS; recipient speaks first."""

    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("an invalid dispatch reached the provider")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        client = BlandClient(KEY, NUMBER, http_client=http)
        with pytest.raises(BlandError, match=r"bland\.arguments_invalid"):
            await client.start_call({**call_payload(), **change})


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


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"queue_status": "busy"}, "busy"),
        ({"queue_status": "no-answer"}, "no_answer"),
        ({"queue_status": "canceled"}, "failed"),
        ({"answered_by": "voicemail"}, "voicemail"),
        ({"answered_by": "machine"}, "voicemail"),
        ({"completed": False, "queue_status": "in-progress"}, "active"),
        ({}, "completed"),
    ],
)
async def test_bland_final_states_stay_distinct(change: dict[str, Any], status: str) -> None:
    """bland-calling.md: no answer, busy, voicemail, a conversation, and failure differ."""

    payload = {**provider_call(), **change}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    ) as http:
        result = await BlandClient(KEY, NUMBER, http_client=http).get_call(CALL_ID)
    assert result["status"] == status


async def test_bland_call_history_is_number_bound_and_paginates() -> None:
    requests: list[httpx.Request] = []
    second = str(UUID(int=28))

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"calls": [{"call_id": CALL_ID}, {"call_id": second}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = BlandClient(KEY, NUMBER, http_client=http)
        full = await client.list_call_ids(
            inbound=True, start_date="2026-09-01T09:00:00-07:00", offset=4, limit=2
        )
        last = await client.list_call_ids(inbound=False, start_date="2026-09-01T16:00:00Z", limit=3)

    assert full == {"provider_call_ids": [CALL_ID, second], "next_offset": 6}
    assert last == {"provider_call_ids": [CALL_ID, second], "next_offset": None}
    inbound, outbound = requests
    assert (inbound.method, str(inbound.url.copy_with(query=None))) == (
        "GET",
        "https://api.bland.ai/v1/calls",
    )
    assert inbound.headers["authorization"] == KEY
    assert dict(inbound.url.params) == {
        "inbound": "true",
        "to_number": NUMBER,
        "start_date": "2026-09-01T16:00:00+00:00",
        "from": "4",
        "limit": "2",
        "ascending": "true",
    }
    assert outbound.url.params["inbound"] == "false"
    assert outbound.url.params["from_number"] == NUMBER
    assert "to_number" not in outbound.url.params


@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        ({"inbound": "true"}, "bland.arguments_invalid"),
        ({"offset": -1}, "bland.arguments_invalid"),
        ({"offset": 1_000_001}, "bland.arguments_invalid"),
        ({"limit": 0}, "bland.arguments_invalid"),
        ({"limit": 26}, "bland.arguments_invalid"),
        ({"start_date": "2026-09-01T09:00:00"}, None),
    ],
)
async def test_bland_call_history_bounds_arguments_before_the_network(
    arguments: dict[str, Any], code: str | None
) -> None:
    def forbidden(request: httpx.Request) -> httpx.Response:
        pytest.fail("an unbounded history read reached the provider")

    async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
        client = BlandClient(KEY, NUMBER, http_client=http)
        with pytest.raises(BlandError) as refused:
            await client.list_call_ids(
                **{"inbound": True, "start_date": "2026-09-01T16:00:00Z", **arguments}
            )
    if code is not None:
        assert refused.value.code == code


@pytest.mark.parametrize(
    "page",
    [
        {},
        {"calls": "not a list"},
        {"calls": [{"call_id": CALL_ID}] * 3},
        {"calls": [{"call_id": "../../account"}]},
        {"calls": ["not an object"]},
    ],
)
async def test_bland_call_history_refuses_malformed_provider_pages(page: dict[str, Any]) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=page))
    ) as http:
        client = BlandClient(KEY, NUMBER, http_client=http)
        with pytest.raises(BlandError, match=r"bland\.provider_output_invalid"):
            await client.list_call_ids(inbound=True, start_date="2026-09-01T16:00:00Z", limit=2)


def _stop_transport(
    requests: list[httpx.Request], call: dict[str, Any], stop: httpx.Response
) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=call) if request.method == "GET" else stop

    return httpx.MockTransport(handle)


@pytest.mark.parametrize(
    "change",
    [{}, {"queue_status": "busy"}, {"answered_by": "voicemail"}],
    ids=["completed", "busy", "voicemail"],
)
async def test_bland_stop_of_an_ended_call_confirms_without_a_provider_mutation(
    change: dict[str, Any],
) -> None:
    requests: list[httpx.Request] = []
    unexpected = httpx.Response(500)
    async with httpx.AsyncClient(
        transport=_stop_transport(requests, {**provider_call(), **change}, unexpected)
    ) as http:
        result = await BlandClient(KEY, NUMBER, http_client=http).stop_call(CALL_ID)
    assert result == {"provider_call_id": CALL_ID, "stopped": True}
    assert [request.method for request in requests] == ["GET"]


async def test_bland_stop_of_an_active_call_terminates_it_at_the_provider() -> None:
    requests: list[httpx.Request] = []
    active = {**provider_call(), "completed": False, "queue_status": "in-progress"}
    stopped = httpx.Response(200, json={"status": "success"})
    async with httpx.AsyncClient(transport=_stop_transport(requests, active, stopped)) as http:
        result = await BlandClient(KEY, NUMBER, http_client=http).stop_call(CALL_ID)
    assert result == {"provider_call_id": CALL_ID, "stopped": True}
    assert [(request.method, request.url.path) for request in requests] == [
        ("GET", f"/v1/calls/{CALL_ID}"),
        ("POST", f"/v1/calls/{CALL_ID}/stop"),
    ]


@pytest.mark.parametrize(
    "stop",
    [httpx.Response(200, json={"status": "error"}), httpx.Response(500, text=KEY)],
    ids=["unconfirmed", "provider-failure"],
)
async def test_bland_unconfirmed_termination_is_reported_uncertain(stop: httpx.Response) -> None:
    """After dispatch, cancellation reports whether termination is confirmed."""

    requests: list[httpx.Request] = []
    active = {**provider_call(), "completed": False, "queue_status": "in-progress"}
    async with httpx.AsyncClient(transport=_stop_transport(requests, active, stop)) as http:
        with pytest.raises(BlandError) as error:
            await BlandClient(KEY, NUMBER, http_client=http).stop_call(CALL_ID)
    assert (error.value.code, error.value.uncertain) == ("bland.outcome_unknown", True)
    assert KEY not in str(error.value)
    assert len(requests) == 2


async def test_bland_stop_refuses_a_call_on_another_number() -> None:
    requests: list[httpx.Request] = []
    foreign = {**provider_call(), "to": "+14155550199", "completed": False}
    async with httpx.AsyncClient(
        transport=_stop_transport(
            requests, foreign, httpx.Response(200, json={"status": "success"})
        )
    ) as http:
        with pytest.raises(BlandError, match=r"bland\.call_not_found"):
            await BlandClient(KEY, NUMBER, http_client=http).stop_call(CALL_ID)
    assert [request.method for request in requests] == ["GET"]
