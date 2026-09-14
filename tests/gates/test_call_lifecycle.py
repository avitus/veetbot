"""Durable call lifecycle gates using a provider that cannot dial."""

import asyncio
import hashlib
import hmac
import json
from dataclasses import replace
from datetime import timedelta
from typing import Any
from uuid import UUID

import pytest

from agent_core.application.calling import CallService
from agent_core.domain.errors import NotFoundError, RunCancelledError
from agent_core.domain.mcp import MCPCallResult
from tests.contract.support import NOW, memory_uow_factory, principal, tool_context
from tests.contract.test_bland_client_contract import CALL_ID, NUMBER, RECIPIENT
from tests.gates.test_call_m27 import call_configuration

SIGNING_FIXTURE = "fixture-webhook-secret"


async def test_completed_call_waits_for_late_summary_without_overwriting_transcript() -> None:
    clock, factory = await memory_uow_factory()
    payload = {**normalized_call(), "summary": "", "summary_complete": False}

    async def provider(server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
        if name == "provider_list_calls":
            return MCPCallResult(structured={"provider_call_ids": [], "next_offset": None})
        return MCPCallResult(structured=payload)

    service = CallService(factory, clock, principal(), call_configuration(), provider)
    await service.receive(*signed_body(), SIGNING_FIXTURE)
    await service.reconcile()
    payload.update(
        summary="Finished processing", summary_complete=True, transcript="Later conflicting text"
    )
    clock.advance(timedelta(minutes=2))
    await service.reconcile()
    value = await service.get_call(principal(), CALL_ID)
    assert value["summary"] == "Finished processing"
    assert value["transcript"] == normalized_call()["transcript"]


async def test_reconciliation_winning_dispatch_response_cannot_regress_completion() -> None:
    clock, factory = await memory_uow_factory()

    async def provider(server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
        if name == "provider_list_calls":
            return MCPCallResult(structured={"provider_call_ids": [CALL_ID], "next_offset": None})
        return MCPCallResult(
            structured={
                **normalized_call(),
                "direction": "outbound",
                "request_id": str(tool_context().invocation_id),
            }
        )

    service = CallService(factory, clock, principal(), call_configuration(), provider)

    async def accepted(args: dict[str, Any]) -> MCPCallResult:
        assert await service.reconcile() == 1
        return MCPCallResult(structured={"provider_call_id": CALL_ID, "status": "accepted"})

    await service.invoke(tool_context(), "bland_call", "start_call", arguments(), accepted)
    async with factory() as uow:
        row = await uow.calls.get(principal(), "dispatch", str(tool_context().invocation_id))
    assert (
        row is not None and row.payload["status"] == "completed" and row.payload["active"] is False
    )


async def test_due_receipts_are_not_starved_by_backoff() -> None:
    clock, factory = await memory_uow_factory()
    fetched: list[str] = []

    async def provider(server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
        if name == "provider_list_calls":
            return MCPCallResult(structured={"provider_call_ids": [], "next_offset": None})
        fetched.append(args["provider_call_id"])
        return MCPCallResult(
            structured={**normalized_call(), "provider_call_id": args["provider_call_id"]}
        )

    service = CallService(factory, clock, principal(), call_configuration(), provider)
    async with factory() as uow:
        for index in range(26):
            await service._put(
                uow.calls,
                "receipt",
                str(UUID(int=index + 1)),
                {
                    "active": True,
                    "status": "pending",
                    "retry_at": (NOW + timedelta(hours=1) if index < 25 else NOW).isoformat(),
                },
            )
    assert await service.reconcile() == 1
    assert fetched == [str(UUID(int=26))]


async def test_transient_provider_outage_does_not_abandon_receipt() -> None:
    clock, factory = await memory_uow_factory()
    service = CallService(factory, clock, principal(), call_configuration())
    async with factory() as uow:
        await service._enqueue(uow.calls, CALL_ID)
    for _ in range(9):
        await service._retry(CALL_ID)
    async with factory() as uow:
        row = await uow.calls.get(principal(), "receipt", CALL_ID)
    assert row is not None and row.payload["active"] is True
    assert row.payload["retry_at"] == (NOW + timedelta(hours=1)).isoformat()


def arguments() -> dict[str, Any]:
    return {
        "phone_number": RECIPIENT,
        "from_number": NUMBER,
        "brief": "Ask about ticket 12.",
        "disclosed_facts": "Ticket 12",
        "config_revision": call_configuration().revision,
        "max_duration_minutes": 5,
        "record": False,
    }


def normalized_call(**changes: Any) -> dict[str, Any]:
    return dict(
        provider_call_id=CALL_ID,
        request_id=None,
        number=NUMBER,
        counterparty=RECIPIENT,
        direction="inbound",
        status="completed",
        created_at=NOW.isoformat(),
        duration_minutes=1.5,
        summary="Caller requests a callback.",
        summary_complete=True,
        transcript="caller: please call me back",
        transcript_complete=True,
        **changes,
    )


def signed_body(call_id: str = CALL_ID) -> tuple[bytes, str]:
    body = json.dumps({"call_id": call_id, "transcript": "never persist webhook content"}).encode()
    return body, hmac.new(SIGNING_FIXTURE.encode(), body, hashlib.sha256).hexdigest()


async def test_outbound_reservation_never_redials_uncertain_dispatch() -> None:
    clock, factory = await memory_uow_factory()
    service = CallService(factory, clock, principal(), call_configuration())
    dispatched: list[dict[str, Any]] = []

    async def lost_response(args: dict[str, Any]) -> MCPCallResult:
        assert not factory.is_open()
        dispatched.append(args)
        raise TimeoutError

    first = await service.invoke(
        tool_context(), "bland_call", "start_call", arguments(), lost_response
    )
    assert first.is_error and first.structured == {"effect_status": "unknown"}
    again = await service.invoke(
        tool_context(), "bland_call", "start_call", arguments(), lost_response
    )
    assert again.is_error and again.structured == {"effect_status": "unknown"}
    other = replace(tool_context(), invocation_id=UUID(int=71))
    blocked = await service.invoke(other, "bland_call", "start_call", arguments(), lost_response)
    assert blocked.is_error and "bland.busy" in blocked.content
    assert len(dispatched) == 1
    assert dispatched[0]["request_id"] == str(tool_context().invocation_id)
    async with factory() as uow:
        records = await uow.calls.list(principal(), "dispatch")
    assert len(records) == 1 and records[0].payload["status"] == "uncertain"
    assert "Ticket 12" not in json.dumps(records[0].payload)


async def test_simultaneous_outbound_calls_admit_only_one() -> None:
    clock, factory = await memory_uow_factory()
    service = CallService(factory, clock, principal(), call_configuration())
    sent = 0

    async def accepted(args: dict[str, Any]) -> MCPCallResult:
        nonlocal sent
        sent += 1
        await asyncio.sleep(0)
        return MCPCallResult(structured={"provider_call_id": CALL_ID, "status": "accepted"})

    results = await asyncio.gather(
        *[
            service.invoke(
                replace(tool_context(), invocation_id=UUID(int=n)),
                "bland_call",
                "start_call",
                arguments(),
                accepted,
            )
            for n in (70, 71)
        ]
    )
    assert sum(not result.is_error for result in results) == sent == 1


async def test_cancelled_or_changed_configuration_cannot_dispatch() -> None:
    clock, factory = await memory_uow_factory()
    service = CallService(factory, clock, principal(), call_configuration())

    class Cancelled:
        def raise_if_cancelled(self) -> None:
            raise RunCancelledError("cancelled")

    async def forbidden(args: dict[str, Any]) -> MCPCallResult:
        pytest.fail("no provider dispatch is permitted")

    with pytest.raises(RunCancelledError):
        await service.invoke(
            replace(tool_context(), cancellation=Cancelled()),
            "bland_call",
            "start_call",
            arguments(),
            forbidden,
        )
    result = await service.invoke(
        tool_context(),
        "bland_call",
        "start_call",
        {**arguments(), "config_revision": "0" * 64},
        forbidden,
    )
    assert result.is_error and result.structured == {"effect_status": "not_applied"}


async def test_signed_intake_deduplicates_then_verifies_provider_ownership() -> None:
    clock, factory = await memory_uow_factory()
    calls: list[str] = []

    async def provider(server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
        assert not factory.is_open()
        calls.append(name)
        if name == "provider_list_calls":
            return MCPCallResult(structured={"provider_call_ids": [], "next_offset": None})
        return MCPCallResult(structured=normalized_call())

    service = CallService(factory, clock, principal(), call_configuration(), provider)
    body, signature = signed_body()
    assert not await service.receive(b"not json", "bad", SIGNING_FIXTURE)
    assert not await service.receive(body, "0" * 64, SIGNING_FIXTURE)
    assert await service.receive(body, signature, SIGNING_FIXTURE)
    assert await service.receive(body, signature, SIGNING_FIXTURE)
    async with factory() as uow:
        receipts = await uow.calls.list(principal(), "receipt")
        assert len(receipts) == 1
        assert "never persist" not in json.dumps(receipts[0].payload)
        assert await uow.calls.list(principal(), "call") == []
    assert await service.reconcile() == 1
    record = await service.get_call(principal(), CALL_ID)
    assert record["trust"] == "EXTERNAL_UNTRUSTED"
    assert record["summary"] == normalized_call()["summary"]
    assert await service.receive(body, signature, SIGNING_FIXTURE)
    assert await service.reconcile() == 0
    assert calls.count("provider_get_call") == 1
    with pytest.raises(NotFoundError):
        await service.get_call(principal().model_copy(update={"principal_id": "attacker"}), CALL_ID)


@pytest.mark.parametrize(
    "field,value",
    [
        ("number", "+14155550199"),
        ("direction", "invalid"),
        ("created_at", "2000-01-01T00:00:00+00:00"),
    ],
)
async def test_foreign_malformed_and_expired_results_store_no_content(
    field: str, value: str
) -> None:
    clock, factory = await memory_uow_factory()

    async def provider(server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
        if name == "provider_list_calls":
            return MCPCallResult(structured={"provider_call_ids": [], "next_offset": None})
        return MCPCallResult(structured={**normalized_call(), field: value})

    service = CallService(factory, clock, principal(), call_configuration(), provider)
    assert await service.receive(*signed_body(), SIGNING_FIXTURE)
    assert await service.reconcile() == 0
    async with factory() as uow:
        assert await uow.calls.list(principal(), "call") == []
        assert "please call me" not in json.dumps(
            (await uow.calls.list(principal(), "receipt"))[0].payload
        )


async def test_owner_stop_is_bound_to_retained_provider_call() -> None:
    clock, factory = await memory_uow_factory()
    stopped: list[str] = []

    async def provider(server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
        if name == "provider_list_calls":
            return MCPCallResult(structured={"provider_call_ids": [], "next_offset": None})
        if name == "provider_stop_call":
            stopped.append(args["provider_call_id"])
            return MCPCallResult(structured={"provider_call_id": CALL_ID, "stopped": True})
        return MCPCallResult(structured={**normalized_call(), "status": "active"})

    service = CallService(factory, clock, principal(), call_configuration(), provider)
    await service.receive(*signed_body(), SIGNING_FIXTURE)
    await service.reconcile()
    assert await service.stop(principal(), CALL_ID) == {
        "call_id": CALL_ID,
        "termination": "confirmed",
    }
    assert stopped == [CALL_ID]
    with pytest.raises(NotFoundError):
        await service.stop(principal().model_copy(update={"tenant_id": "other"}), CALL_ID)
    assert stopped == [CALL_ID]


async def test_completed_call_notification_is_content_free_and_exactly_once() -> None:
    clock, factory = await memory_uow_factory()

    async def provider(server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
        if name == "provider_list_calls":
            return MCPCallResult(structured={"provider_call_ids": [], "next_offset": None})
        return MCPCallResult(structured=normalized_call())

    service = CallService(
        factory, clock, principal(), call_configuration(), provider, notifications=True
    )
    await service.receive(*signed_body(), SIGNING_FIXTURE)
    await service.reconcile()
    await service.receive(*signed_body(), SIGNING_FIXTURE)
    await service.reconcile()
    async with factory() as uow:
        items = await uow.notification_outbox.list(principal(), limit=10)
    assert len(items) == 1
    assert items[0].kind.value == "call_finished"
    payload = items[0].payload.model_dump(mode="json", exclude_none=True)
    assert payload == {
        "version": 1,
        "kind": "call_finished",
        "title": "New call result",
        "call_id": CALL_ID,
        "notification_id": str(items[0].id),
    }
    assert "callback" not in json.dumps(payload)


async def test_erased_call_cannot_be_resurrected_by_a_valid_callback() -> None:
    clock, factory = await memory_uow_factory()

    async def provider(server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
        if name == "provider_list_calls":
            return MCPCallResult(structured={"provider_call_ids": [], "next_offset": None})
        return MCPCallResult(structured=normalized_call())

    service = CallService(factory, clock, principal(), call_configuration(), provider)
    await service.receive(*signed_body(), SIGNING_FIXTURE)
    await service.reconcile()
    deleted = await service.delete(principal(), CALL_ID)
    assert deleted["erased"] is True and deleted["provider_deleted"] is False
    await service.receive(*signed_body(), SIGNING_FIXTURE)
    await service.reconcile()
    assert (await service.get_call(principal(), CALL_ID))["erased"] is True
    async with factory() as uow:
        row = await uow.calls.get(principal(), "call", CALL_ID)
    assert row is not None
    assert "transcript" not in row.payload and "summary" not in row.payload


async def test_worker_expires_content_after_thirty_days() -> None:
    clock, factory = await memory_uow_factory()

    async def provider(server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
        if name == "provider_list_calls":
            return MCPCallResult(structured={"provider_call_ids": [], "next_offset": None})
        return MCPCallResult(structured=normalized_call())

    service = CallService(factory, clock, principal(), call_configuration(), provider)
    await service.receive(*signed_body(), SIGNING_FIXTURE)
    await service.reconcile()
    clock.advance(timedelta(days=31))
    await service.reconcile()
    async with factory() as uow:
        row = await uow.calls.get(principal(), "call", CALL_ID)
    assert row is not None and row.payload.get("erased") is True


async def test_cancelled_run_terminates_a_recovered_uncertain_call_without_redial() -> None:
    from agent_core.domain.runs import RunStatus
    from tests.contract.support import run

    clock, factory = await memory_uow_factory()
    stopped: list[str] = []

    async def lost(args: dict[str, Any]) -> MCPCallResult:
        raise TimeoutError

    async def provider(server: str, name: str, args: dict[str, Any]) -> MCPCallResult:
        if name == "provider_list_calls":
            return MCPCallResult(
                structured={
                    "provider_call_ids": [CALL_ID] if not args["inbound"] else [],
                    "next_offset": None,
                }
            )
        if name == "provider_stop_call":
            stopped.append(args["provider_call_id"])
            return MCPCallResult(structured={"provider_call_id": CALL_ID, "stopped": True})
        return MCPCallResult(
            structured={
                **normalized_call(),
                "status": "active",
                "direction": "outbound",
                "request_id": str(tool_context().invocation_id),
            }
        )

    service = CallService(factory, clock, principal(), call_configuration(), provider)
    await service.invoke(tool_context(), "bland_call", "start_call", arguments(), lost)
    async with factory() as uow:
        await uow.runs.create(
            run(status=RunStatus.CANCELLED).model_copy(update={"cancel_requested_at": NOW})
        )
    await service.reconcile()
    assert stopped == [CALL_ID]
    async with factory() as uow:
        dispatch = await uow.calls.get(principal(), "dispatch", str(tool_context().invocation_id))
    assert dispatch is not None and dispatch.payload.get("termination") == "confirmed"
