"""Milestone 14 Telegram transport and polling gates."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from agent_core.adapters.determinism import FixedClock
from agent_core.application.surface_worker import SurfaceWorker
from agent_core.application.trajectory_service import TrajectoryRedactor
from agent_core.domain.devices import PushProvider
from agent_core.domain.security import SECRET_RULES
from agent_core.domain.surfaces import SurfaceMessageKind, SurfaceTransportOutcome

NOW = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
SURFACE_ID = UUID("00000000-0000-4000-8000-000000001490")
TOKEN_CORPUS = Path(__file__).resolve().parents[1] / "corpora/surface_token_leak"


def _adapter() -> Any:
    spec = importlib.util.find_spec("agent_core.adapters.telegram")
    assert spec is not None, "Milestone 14 Telegram adapter has not been implemented"
    from agent_core.adapters import telegram

    return telegram


def test_telegram_adapter_exists() -> None:
    _adapter()


async def test_surface_worker_runs_poller_only_in_its_dedicated_loop() -> None:
    class Poller:
        run_once_calls = 0
        forever_calls = 0

        def stop(self) -> None:
            return None

        async def run_once(self) -> int:
            self.run_once_calls += 1
            return 0

        async def run_forever(self) -> None:
            self.forever_calls += 1
            await asyncio.Event().wait()

    poller = Poller()
    dispatch_calls = 0
    worker: SurfaceWorker

    async def dispatch() -> int:
        nonlocal dispatch_calls
        dispatch_calls += 1
        worker.stop()
        return 0

    worker = SurfaceWorker(
        dispatch_once=dispatch,
        clock=FixedClock(NOW),
        fallback_poll_seconds=1,
        additional_services=(poller,),
    )

    await worker.run_forever()

    assert dispatch_calls == 1
    assert poller.forever_calls == 1
    assert poller.run_once_calls == 0


async def test_telegram_transport_normalizes_private_updates_and_uses_fixed_origin() -> None:
    telegram = _adapter()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        if method == "deleteWebhook":
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "getUpdates":
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "result": [
                        {
                            "update_id": 41,
                            "message": {
                                "message_id": 9,
                                "date": int(NOW.timestamp()),
                                "chat": {"id": 12345, "type": "private"},
                                "from": {
                                    "id": 12345,
                                    "first_name": "Avi",
                                    "username": "owner",
                                },
                                "text": "Hello Veetbot",
                            },
                        }
                    ],
                },
            )
        raise AssertionError(f"unexpected Telegram method {method}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = telegram.TelegramBotTransport(
        token=SecretStr("123456789:" + "telegram-test-token-value"),
        client=client,
    )
    try:
        assert await transport.delete_webhook() is True
        updates = await transport.get_updates(
            surface_id=SURFACE_ID,
            offset=41,
            timeout_seconds=30,
        )
    finally:
        await client.aclose()

    assert len(updates) == 1
    assert updates[0].provider is PushProvider.TELEGRAM
    assert updates[0].external_update_id == "41"
    assert updates[0].sender_id == "12345"
    assert updates[0].chat_ref == "12345"
    assert updates[0].text == "Hello Veetbot"
    assert updates[0].message_kind is SurfaceMessageKind.TEXT
    assert all(request.url.scheme == "https" for request in requests)
    assert all(request.url.host == "api.telegram.org" for request in requests)
    assert requests[1].url.params["offset"] == "41"
    assert requests[1].url.params["timeout"] == "30"
    assert json.loads(requests[0].content) == {"drop_pending_updates": False}


async def test_telegram_send_maps_provider_failure_without_leaking_token_or_body() -> None:
    telegram = _adapter()
    provider_body = "provider body 123456789:" + "telegram-test-token-value"

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, text=provider_body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = telegram.TelegramBotTransport(
        token=SecretStr("123456789:" + "telegram-test-token-value"),
        client=client,
    )
    try:
        result = await transport.send_text("12345", "Hello")
    finally:
        await client.aclose()

    assert result.outcome is SurfaceTransportOutcome.RETRY
    assert result.reason_code == "surface.transport_unavailable"
    assert "telegram-test-token-value" not in repr(result)
    assert provider_body not in repr(result)


async def test_telegram_network_exception_drops_the_token_bearing_cause() -> None:
    telegram = _adapter()
    token = "123456789:" + "telegram-test-token-value"

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("synthetic connection failure", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = telegram.TelegramBotTransport(token=SecretStr(token), client=client)
    try:
        with pytest.raises(telegram.TelegramTransportError) as raised:
            await transport.get_updates(
                surface_id=SURFACE_ID,
                offset=0,
                timeout_seconds=30,
            )
    finally:
        await client.aclose()

    assert raised.value.__cause__ is None
    assert token not in repr(raised.value)


async def test_poller_resumes_from_committed_receipt_and_dedupes_through_ingress() -> None:
    telegram = _adapter()
    offsets: list[int] = []
    ingested: list[str] = []
    successful_offsets: list[int] = []

    class FakeTransport:
        async def delete_webhook(self) -> bool:
            return True

        async def get_updates(self, *, surface_id: UUID, offset: int, timeout_seconds: int):  # type: ignore[no-untyped-def]
            del surface_id, timeout_seconds
            offsets.append(offset)
            if len(offsets) > 1:
                return ()
            return (
                telegram.normalized_telegram_update(
                    {
                        "update_id": 41,
                        "message": {
                            "date": int(NOW.timestamp()),
                            "chat": {"id": 12345, "type": "private"},
                            "from": {"id": 12345, "first_name": "Owner"},
                            "text": "one",
                        },
                    },
                    surface_id=SURFACE_ID,
                ),
                telegram.normalized_telegram_update(
                    {
                        "update_id": 42,
                        "message": {
                            "date": int(NOW.timestamp()),
                            "chat": {"id": 12345, "type": "private"},
                            "from": {"id": 12345, "first_name": "Owner"},
                            "text": "two",
                        },
                    },
                    surface_id=SURFACE_ID,
                ),
            )

    committed = 40

    async def latest() -> int | None:
        return committed

    async def ingest(update):  # type: ignore[no-untyped-def]
        nonlocal committed
        ingested.append(update.external_update_id)
        committed = int(update.external_update_id)

    async def poll_succeeded(offset: int) -> None:
        successful_offsets.append(offset)

    poller = telegram.TelegramPoller(
        surface_id=SURFACE_ID,
        transport=FakeTransport(),
        latest_committed_update_id=latest,
        ingest=ingest,
        poll_lock=telegram.InMemoryTelegramPollLock(),
        timeout_seconds=30,
        fallback_poll_seconds=1,
        poll_succeeded=poll_succeeded,
    )

    assert await poller.run_once() == 2
    assert await poller.run_once() == 0
    await poller.aclose()

    assert offsets == [41, 43]
    assert successful_offsets == [41, 43]
    assert ingested == ["41", "42"]


async def test_telegram_token_leak_corpus_is_closed_across_transport_and_redaction(
    caplog: pytest.LogCaptureFixture,
) -> None:
    telegram = _adapter()
    members = sorted(TOKEN_CORPUS.glob("*.json"))
    assert len(members) >= 8
    values: list[str] = []
    for member in members:
        document = json.loads(member.read_text(encoding="utf-8"))
        token = "".join(document["parts"])
        values.append(token)
        assert SECRET_RULES["telegram_bot_token"].search(token)

        def handler(
            request: httpx.Request,
            leaked_token: str = token,
        ) -> httpx.Response:
            del request
            return httpx.Response(503, text=f"provider echoed {leaked_token}")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        transport = telegram.TelegramBotTransport(token=SecretStr(token), client=client)
        try:
            result = await transport.send_text("12345", "hello")
        finally:
            await client.aclose()
        assert result.reason_code == "surface.transport_unavailable"
        assert token not in repr(result)

        payload: dict[str, object] = {"text": token}
        TrajectoryRedactor().redact([payload])
        assert token not in json.dumps(payload)

    emitted = caplog.text
    assert all(token not in emitted for token in values)
