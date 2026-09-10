"""Official Meta WhatsApp Business Cloud API webhook and outbound adapter."""

from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

import httpx
from fastapi import FastAPI, Request, Response
from pydantic import SecretStr

from agent_core.domain.devices import PushProvider
from agent_core.domain.surfaces import (
    SurfaceChatKind,
    SurfaceInboundMessage,
    SurfaceMessageKind,
    SurfaceTransportOutcome,
    SurfaceTransportResult,
)

META_GRAPH_ORIGIN = "https://graph.facebook.com"
WHATSAPP_WEBHOOK_PATH = "/webhooks/whatsapp"
WHATSAPP_FREEFORM_WINDOW = timedelta(hours=24)
_SAFE_GRAPH_VERSION = re.compile(r"^v[1-9][0-9]*\.[0-9]+$")
_SAFE_PHONE_NUMBER_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

type WhatsAppIngest = Callable[[SurfaceInboundMessage], Awaitable[object] | object]


class WhatsAppTransport(Protocol):
    async def send_text(self, chat_ref: str, text: str) -> SurfaceTransportResult: ...

    async def send_update_template(self, chat_ref: str) -> SurfaceTransportResult: ...


def _empty(status_code: int) -> Response:
    return Response(status_code=status_code, content=b"", media_type=None)


def _signature_matches(app_secret: SecretStr, body: bytes, supplied: str | None) -> bool:
    expected = (
        "sha256="
        + hmac.new(app_secret.get_secret_value().encode("utf-8"), body, hashlib.sha256).hexdigest()
    )
    candidate = supplied if supplied is not None else ""
    return hmac.compare_digest(expected.encode("ascii"), candidate.encode("utf-8"))


def _sender_labels(value: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    contacts = value.get("contacts")
    if not isinstance(contacts, list):
        return labels
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        wa_id = contact.get("wa_id")
        profile = contact.get("profile")
        name = profile.get("name") if isinstance(profile, dict) else None
        if isinstance(wa_id, str) and isinstance(name, str) and name.strip():
            labels[wa_id] = name[:255]
    return labels


def _normalized_messages(
    payload: object,
    *,
    surface_id: UUID,
    phone_number_id: str,
) -> tuple[SurfaceInboundMessage, ...]:
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        raise ValueError("whatsapp payload object is invalid")
    normalized: list[SurfaceInboundMessage] = []
    entries = payload.get("entry")
    if not isinstance(entries, list):
        raise ValueError("whatsapp payload entries are invalid")
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            metadata = value.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("phone_number_id") != phone_number_id:
                continue
            labels = _sender_labels(value)
            messages = value.get("messages")
            if messages is None:
                continue
            if not isinstance(messages, list):
                raise ValueError("whatsapp messages are invalid")
            for message in messages:
                if not isinstance(message, dict):
                    continue
                message_id = message.get("id")
                sender_id = message.get("from")
                timestamp = message.get("timestamp")
                message_type = message.get("type")
                if (
                    not isinstance(message_id, str)
                    or not isinstance(sender_id, str)
                    or not isinstance(timestamp, str)
                ):
                    raise ValueError("whatsapp message identity is invalid")
                try:
                    received_at = datetime.fromtimestamp(int(timestamp), tz=UTC)
                except (OverflowError, ValueError) as exc:
                    raise ValueError("whatsapp message timestamp is invalid") from exc
                text: str | None = None
                kind = SurfaceMessageKind.MEDIA
                if message_type == "text":
                    text_object = message.get("text")
                    body = text_object.get("body") if isinstance(text_object, dict) else None
                    if not isinstance(body, str) or not body.strip():
                        raise ValueError("whatsapp text body is invalid")
                    text = body
                    kind = SurfaceMessageKind.TEXT
                normalized.append(
                    SurfaceInboundMessage(
                        surface_id=surface_id,
                        provider=PushProvider.WHATSAPP,
                        external_update_id=message_id,
                        sender_id=sender_id,
                        sender_label=labels.get(sender_id),
                        chat_ref=sender_id,
                        chat_kind=SurfaceChatKind.DIRECT,
                        message_kind=kind,
                        text=text,
                        received_at=received_at,
                    )
                )
    return tuple(normalized)


def create_whatsapp_webhook_app(
    *,
    surface_id: UUID,
    phone_number_id: str,
    app_secret: SecretStr,
    verify_token: SecretStr,
    ingest: WhatsAppIngest,
    max_body_bytes: int = 1_048_576,
) -> FastAPI:
    """Build the loopback-only ASGI listener; binding is owned by the role runner."""

    if not _SAFE_PHONE_NUMBER_ID.fullmatch(phone_number_id):
        raise ValueError("WhatsApp phone number id is invalid")
    if max_body_bytes <= 0:
        raise ValueError("WhatsApp webhook body bound must be positive")
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    @app.get(WHATSAPP_WEBHOOK_PATH, response_model=None)
    async def handshake(request: Request) -> Response:
        mode = request.query_params.get("hub.mode", "")
        supplied = request.query_params.get("hub.verify_token", "")
        challenge = request.query_params.get("hub.challenge", "")
        expected = verify_token.get_secret_value()
        if (
            mode != "subscribe"
            or not challenge
            or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
        ):
            return _empty(403)
        return Response(content=challenge, status_code=200, media_type="text/plain")

    @app.post(WHATSAPP_WEBHOOK_PATH, response_model=None)
    async def webhook(request: Request) -> Response:
        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > max_body_bytes:
                    return _empty(413)
            except ValueError:
                return _empty(400)
        chunks: list[bytes] = []
        body_size = 0
        async for chunk in request.stream():
            body_size += len(chunk)
            if body_size > max_body_bytes:
                return _empty(413)
            chunks.append(chunk)
        body = b"".join(chunks)
        if not _signature_matches(app_secret, body, request.headers.get("x-hub-signature-256")):
            return _empty(401)
        try:
            payload = json.loads(body)
            messages = _normalized_messages(
                payload,
                surface_id=surface_id,
                phone_number_id=phone_number_id,
            )
        except (UnicodeDecodeError, ValueError):
            return _empty(400)
        for message in messages:
            result = ingest(message)
            if inspect.isawaitable(result):
                await result
        return _empty(200)

    return app


class WhatsAppCloudTransport:
    """Outbound client confined to Meta's fixed Graph origin."""

    def __init__(
        self,
        *,
        access_token: SecretStr,
        phone_number_id: str,
        graph_api_version: str,
        template_name: str = "veetbot_update_available",
        template_language: str = "en_US",
        client: httpx.AsyncClient | None = None,
        max_response_bytes: int = 65_536,
    ) -> None:
        if not access_token.get_secret_value():
            raise ValueError("WhatsApp access token is empty")
        if not _SAFE_PHONE_NUMBER_ID.fullmatch(phone_number_id):
            raise ValueError("WhatsApp phone number id is invalid")
        if not _SAFE_GRAPH_VERSION.fullmatch(graph_api_version):
            raise ValueError("WhatsApp Graph API version is invalid")
        if not template_name or len(template_name) > 512:
            raise ValueError("WhatsApp template name is invalid")
        if not template_language or len(template_language) > 32:
            raise ValueError("WhatsApp template language is invalid")
        if max_response_bytes <= 0:
            raise ValueError("WhatsApp response bound must be positive")
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self._graph_api_version = graph_api_version
        self._template_name = template_name
        self._template_language = template_language
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._max_response_bytes = max_response_bytes

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def send_text(self, chat_ref: str, text: str) -> SurfaceTransportResult:
        if not text:
            raise ValueError("WhatsApp text cannot be empty")
        return await self._send(
            chat_ref,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": chat_ref,
                "type": "text",
                "text": {"preview_url": False, "body": text},
            },
        )

    async def send_update_template(self, chat_ref: str) -> SurfaceTransportResult:
        return await self._send(
            chat_ref,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": chat_ref,
                "type": "template",
                "template": {
                    "name": self._template_name,
                    "language": {"code": self._template_language},
                },
            },
        )

    async def _send(self, chat_ref: str, payload: dict[str, Any]) -> SurfaceTransportResult:
        if not chat_ref or len(chat_ref) > 255:
            raise ValueError("WhatsApp chat reference is invalid")
        url = f"{META_GRAPH_ORIGIN}/{self._graph_api_version}/{self._phone_number_id}/messages"
        try:
            request = self._client.build_request(
                "POST",
                url,
                headers={
                    "Authorization": f"Bearer {self._access_token.get_secret_value()}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response = await self._client.send(request, stream=True, follow_redirects=False)
            try:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > self._max_response_bytes:
                        return SurfaceTransportResult(
                            outcome=SurfaceTransportOutcome.REJECTED,
                            reason_code="surface.response_too_large",
                        )
                    chunks.append(chunk)
                if response.status_code == 429 or response.status_code >= 500:
                    return SurfaceTransportResult(
                        outcome=SurfaceTransportOutcome.RETRY,
                        reason_code="surface.transport_unavailable",
                    )
                if response.status_code < 200 or response.status_code >= 300:
                    return SurfaceTransportResult(
                        outcome=SurfaceTransportOutcome.REJECTED,
                        reason_code="surface.transport_rejected",
                    )
                try:
                    payload_out = json.loads(b"".join(chunks))
                except (UnicodeError, ValueError):
                    return SurfaceTransportResult(
                        outcome=SurfaceTransportOutcome.REJECTED,
                        reason_code="surface.transport_invalid_response",
                    )
            finally:
                await response.aclose()
            if not isinstance(payload_out, dict):
                return SurfaceTransportResult(
                    outcome=SurfaceTransportOutcome.REJECTED,
                    reason_code="surface.transport_invalid_response",
                )
            messages = payload_out.get("messages")
            if not isinstance(messages, list) or not messages or not isinstance(messages[0], dict):
                return SurfaceTransportResult(
                    outcome=SurfaceTransportOutcome.REJECTED,
                    reason_code="surface.transport_invalid_response",
                )
            candidate = messages[0].get("id")
            if not isinstance(candidate, str) or not candidate:
                return SurfaceTransportResult(
                    outcome=SurfaceTransportOutcome.REJECTED,
                    reason_code="surface.transport_invalid_response",
                )
            return SurfaceTransportResult(
                outcome=SurfaceTransportOutcome.DELIVERED,
                external_message_id=candidate,
            )
        except httpx.HTTPError:
            return SurfaceTransportResult(
                outcome=SurfaceTransportOutcome.RETRY,
                reason_code="surface.transport_unavailable",
            )


class WhatsAppDeliveryService:
    """Enforce Meta's freeform window before transport selection."""

    def __init__(self, *, transport: WhatsAppTransport) -> None:
        self._transport = transport

    def freeform_allowed(
        self,
        *,
        last_inbound_at: datetime | None,
        now: datetime,
    ) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("WhatsApp delivery time must be aware")
        if last_inbound_at is None:
            return False
        if last_inbound_at.tzinfo is None or last_inbound_at.utcoffset() is None:
            raise ValueError("WhatsApp inbound time must be aware")
        return now.astimezone(UTC) - last_inbound_at.astimezone(UTC) <= WHATSAPP_FREEFORM_WINDOW

    async def deliver_template(self, chat_ref: str) -> SurfaceTransportResult:
        return await self._transport.send_update_template(chat_ref)

    async def deliver(
        self,
        chat_ref: str,
        text: str,
        *,
        last_inbound_at: datetime | None,
        now: datetime,
    ) -> SurfaceTransportResult:
        if self.freeform_allowed(last_inbound_at=last_inbound_at, now=now):
            return await self._transport.send_text(chat_ref, text)
        return await self.deliver_template(chat_ref)
