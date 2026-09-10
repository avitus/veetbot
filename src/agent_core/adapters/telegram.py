"""Telegram Bot API transport and restart-safe long poller."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

import httpx
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from agent_core.domain.devices import PushProvider, PushTarget
from agent_core.domain.notifications import DeliveryOutcome, PushMessage, PushOutcome
from agent_core.domain.surfaces import (
    SurfaceChatKind,
    SurfaceInboundMessage,
    SurfaceMessageKind,
    SurfaceTransportOutcome,
    SurfaceTransportResult,
)

TELEGRAM_API_ORIGIN = "https://api.telegram.org"
_MAX_RESPONSE_BYTES = 65_536
logger = logging.getLogger(__name__)

type TelegramIngest = Callable[[SurfaceInboundMessage], Awaitable[object]]
type LatestCommittedUpdate = Callable[[], Awaitable[int | None]]
type TelegramPollSucceeded = Callable[[int], Awaitable[None]]


class TelegramTransportError(RuntimeError):
    """A closed transport failure safe to log without a token-bearing URL."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


class TelegramPollLock(Protocol):
    async def acquire(self) -> bool: ...

    async def release(self) -> None: ...


class TelegramPollingTransport(Protocol):
    async def delete_webhook(self) -> bool: ...

    async def get_updates(
        self,
        *,
        surface_id: UUID,
        offset: int,
        timeout_seconds: int,
    ) -> tuple[SurfaceInboundMessage, ...]: ...


class InMemoryTelegramPollLock:
    def __init__(self) -> None:
        self._held = False

    async def acquire(self) -> bool:
        if self._held:
            return True
        self._held = True
        return True

    async def release(self) -> None:
        self._held = False


class PostgresTelegramPollLock:
    """Hold one session advisory lock for this bot for the process lifetime."""

    def __init__(self, engine: AsyncEngine, *, tenant_id: str, surface_id: UUID) -> None:
        self._engine = engine
        self._key = f"telegram-poll:{tenant_id}:{surface_id}"
        self._connection: AsyncConnection | None = None

    async def acquire(self) -> bool:
        if self._connection is not None:
            return True
        connection = await self._engine.connect()
        acquired = bool(
            await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtextextended(:key, 0))"),
                {"key": self._key},
            )
        )
        if not acquired:
            await connection.close()
            return False
        self._connection = connection
        return True

    async def release(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            await connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:key, 0))"),
                {"key": self._key},
            )
        finally:
            await connection.close()


def normalized_telegram_update(
    update: object,
    *,
    surface_id: UUID,
) -> SurfaceInboundMessage:
    if not isinstance(update, dict):
        raise ValueError("Telegram update is invalid")
    update_id = update.get("update_id")
    message = update.get("message")
    if not isinstance(update_id, int) or isinstance(update_id, bool) or update_id < 0:
        raise ValueError("Telegram update identifier is invalid")
    if not isinstance(message, dict):
        raise ValueError("Telegram message is invalid")
    chat = message.get("chat")
    sender = message.get("from")
    timestamp = message.get("date")
    if not isinstance(chat, dict) or not isinstance(sender, dict):
        raise ValueError("Telegram sender identity is invalid")
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    sender_id = sender.get("id")
    if (
        not isinstance(chat_id, int)
        or isinstance(chat_id, bool)
        or not isinstance(sender_id, int)
        or isinstance(sender_id, bool)
        or not isinstance(chat_type, str)
        or not isinstance(timestamp, int)
        or isinstance(timestamp, bool)
    ):
        raise ValueError("Telegram message identity is invalid")
    try:
        received_at = datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("Telegram message timestamp is invalid") from exc
    label_parts = [sender.get("first_name"), sender.get("last_name")]
    sender_label = " ".join(
        part.strip() for part in label_parts if isinstance(part, str) and part.strip()
    )
    if not sender_label:
        username = sender.get("username")
        sender_label = username.strip() if isinstance(username, str) else ""
    message_text = message.get("text")
    if isinstance(message_text, str) and message_text.strip():
        message_kind = SurfaceMessageKind.TEXT
        normalized_text: str | None = message_text
    else:
        message_kind = SurfaceMessageKind.MEDIA
        normalized_text = None
    return SurfaceInboundMessage(
        surface_id=surface_id,
        provider=PushProvider.TELEGRAM,
        external_update_id=str(update_id),
        sender_id=str(sender_id),
        sender_label=sender_label[:255] or None,
        chat_ref=str(chat_id),
        chat_kind=(SurfaceChatKind.DIRECT if chat_type == "private" else SurfaceChatKind.GROUP),
        message_kind=message_kind,
        text=normalized_text,
        received_at=received_at,
    )


class TelegramBotTransport:
    """Token-confined client for the fixed Telegram Bot API origin."""

    def __init__(
        self,
        *,
        token: SecretStr,
        client: httpx.AsyncClient | None = None,
        max_response_bytes: int = _MAX_RESPONSE_BYTES,
    ) -> None:
        token_value = token.get_secret_value()
        if (
            not token_value
            or "/" in token_value
            or any(character.isspace() for character in token_value)
        ):
            raise ValueError("Telegram bot token is invalid")
        if max_response_bytes <= 0:
            raise ValueError("Telegram response bound must be positive")
        self._token = token
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(60.0),
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._max_response_bytes = max_response_bytes

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API_ORIGIN}/bot{self._token.get_secret_value()}/{method}"

    async def _request(
        self,
        method: str,
        *,
        payload: dict[str, object] | None = None,
        query: dict[str, str | int] | None = None,
    ) -> object:
        request = self._client.build_request(
            "GET" if query is not None else "POST",
            self._url(method),
            params=query,
            json=payload,
        )
        try:
            response = await self._client.send(request, stream=True, follow_redirects=False)
            try:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._max_response_bytes:
                        raise TelegramTransportError("surface.response_too_large")
                    chunks.append(chunk)
                if response.status_code == 429 or response.status_code >= 500:
                    raise TelegramTransportError("surface.transport_unavailable")
                if response.status_code < 200 or response.status_code >= 300:
                    raise TelegramTransportError("surface.transport_rejected")
                document = json.loads(b"".join(chunks))
            finally:
                await response.aclose()
        except TelegramTransportError:
            raise
        except (httpx.HTTPError, UnicodeError, ValueError):
            raise TelegramTransportError("surface.transport_unavailable") from None
        if not isinstance(document, dict) or document.get("ok") is not True:
            raise TelegramTransportError("surface.transport_invalid_response")
        return document.get("result")

    async def delete_webhook(self) -> bool:
        result = await self._request(
            "deleteWebhook",
            payload={"drop_pending_updates": False},
        )
        if not isinstance(result, bool):
            raise TelegramTransportError("surface.transport_invalid_response")
        return result

    async def get_updates(
        self,
        *,
        surface_id: UUID,
        offset: int,
        timeout_seconds: int,
    ) -> tuple[SurfaceInboundMessage, ...]:
        if offset < 0 or timeout_seconds <= 0 or timeout_seconds > 50:
            raise ValueError("Telegram poll bounds are invalid")
        result = await self._request(
            "getUpdates",
            query={
                "offset": offset,
                "timeout": timeout_seconds,
                "allowed_updates": json.dumps(["message"], separators=(",", ":")),
            },
        )
        if not isinstance(result, list):
            raise TelegramTransportError("surface.transport_invalid_response")
        messages: list[SurfaceInboundMessage] = []
        for update in result:
            if isinstance(update, dict) and "message" not in update:
                continue
            try:
                messages.append(normalized_telegram_update(update, surface_id=surface_id))
            except ValueError:
                raise TelegramTransportError("surface.transport_invalid_response") from None
        return tuple(messages)

    async def send_text(self, chat_ref: str, text_value: str) -> SurfaceTransportResult:
        if not chat_ref or len(chat_ref) > 255 or not text_value:
            raise ValueError("Telegram outbound message is invalid")
        try:
            result = await self._request(
                "sendMessage",
                payload={"chat_id": chat_ref, "text": text_value},
            )
        except TelegramTransportError as exc:
            outcome = (
                SurfaceTransportOutcome.RETRY
                if exc.reason_code == "surface.transport_unavailable"
                else SurfaceTransportOutcome.REJECTED
            )
            return SurfaceTransportResult(outcome=outcome, reason_code=exc.reason_code)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return SurfaceTransportResult(
            outcome=SurfaceTransportOutcome.DELIVERED,
            external_message_id=(str(message_id) if isinstance(message_id, int) else None),
        )

    async def deliver(self, target: PushTarget, message: PushMessage) -> PushOutcome:
        result = await self.send_text(
            target.token.get_secret_value(),
            "Veetbot has an update available.",
        )
        outcome = {
            SurfaceTransportOutcome.DELIVERED: DeliveryOutcome.DELIVERED,
            SurfaceTransportOutcome.RETRY: DeliveryOutcome.RETRY,
            SurfaceTransportOutcome.REJECTED: DeliveryOutcome.REJECTED,
        }[result.outcome]
        return PushOutcome(
            outcome=outcome,
            provider_reason=result.reason_code,
            provider_id=result.external_message_id,
        )


class TelegramPoller:
    """Long-poll one bot, deriving every offset from committed receipts."""

    def __init__(
        self,
        *,
        surface_id: UUID,
        transport: TelegramPollingTransport,
        latest_committed_update_id: LatestCommittedUpdate,
        ingest: TelegramIngest,
        poll_lock: TelegramPollLock,
        timeout_seconds: int,
        fallback_poll_seconds: float,
        poll_succeeded: TelegramPollSucceeded | None = None,
    ) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 50 or fallback_poll_seconds <= 0:
            raise ValueError("Telegram poll limits are invalid")
        self._surface_id = surface_id
        self._transport = transport
        self._latest_committed_update_id = latest_committed_update_id
        self._ingest = ingest
        self._poll_lock = poll_lock
        self._timeout_seconds = timeout_seconds
        self._fallback_poll_seconds = fallback_poll_seconds
        self._poll_succeeded = poll_succeeded
        self._initialized = False
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    async def run_once(self) -> int:
        if not await self._poll_lock.acquire():
            return 0
        if not self._initialized:
            if not await self._transport.delete_webhook():
                raise TelegramTransportError("surface.transport_rejected")
            self._initialized = True
        latest = await self._latest_committed_update_id()
        offset = 0 if latest is None else latest + 1
        updates = await self._transport.get_updates(
            surface_id=self._surface_id,
            offset=offset,
            timeout_seconds=self._timeout_seconds,
        )
        if self._poll_succeeded is not None:
            await self._poll_succeeded(offset)
        for update in updates:
            if self._stopping:
                break
            await self._ingest(update)
        return len(updates)

    async def run_forever(self) -> None:
        while not self._stopping:
            try:
                processed = await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "surface_telegram_poll_failed",
                    extra={"error_class": type(exc).__name__},
                )
                processed = 0
            if processed == 0 and not self._stopping:
                await asyncio.sleep(self._fallback_poll_seconds)

    async def aclose(self) -> None:
        self.stop()
        await self._poll_lock.release()
