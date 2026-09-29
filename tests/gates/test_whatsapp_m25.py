"""Milestone 25 WhatsApp boundary and transport gates."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from agent_core.adapters.whatsapp import (
    META_GRAPH_ORIGIN,
    WhatsAppCloudTransport,
    WhatsAppDeliveryService,
    create_whatsapp_webhook_app,
)
from agent_core.application.trajectory_service import TrajectoryRedactor
from agent_core.domain.devices import DeviceKind, PushProvider, device_routing_issue
from agent_core.domain.security import SECRET_RULES
from agent_core.domain.surfaces import (
    SurfaceInboundMessage,
    SurfaceMessageKind,
    SurfaceTransportOutcome,
    SurfaceTransportResult,
)

NOW = datetime(2026, 9, 10, 22, 0, tzinfo=UTC)
SURFACE_ID = UUID("00000000-0000-4000-8000-000000002500")
APP_SIGNATURE_KEY = "app-secret-value"
VERIFICATION_CHALLENGE = "verify-token-value"


def test_whatsapp_secrets_have_dedicated_scanner_and_redaction_families() -> None:
    names = {
        "whatsapp_access_token",
        "whatsapp_app_secret",
        "whatsapp_verify_token",
    }
    assert names <= SECRET_RULES.keys()
    samples = {
        "whatsapp_access_token": "EA" + ("A" * 30),
        "whatsapp_app_secret": "whatsapp_app_secret=" + ("ab" * 16),
        "whatsapp_verify_token": "whatsapp_verify_token=" + ("V" * 24),
    }
    for name, sample in samples.items():
        assert SECRET_RULES[name].search(sample)

    payload: dict[str, Any] = {"text": "\n".join(samples.values())}
    summary = TrajectoryRedactor().redact([payload])

    assert all(sample not in json.dumps(payload) for sample in samples.values())
    assert names <= summary.replacements.keys()


def test_whatsapp_is_a_surface_only_push_provider() -> None:
    assert hasattr(PushProvider, "WHATSAPP"), "WhatsApp provider is not implemented"
    provider = PushProvider("whatsapp")

    assert (
        device_routing_issue(
            kind=DeviceKind.SURFACE,
            provider=provider,
            token_present=True,
            environment=None,
            app_bundle_id_present=False,
        )
        is None
    )
    issue = device_routing_issue(
        kind=DeviceKind.MOBILE,
        provider=provider,
        token_present=True,
        environment=None,
        app_bundle_id_present=False,
    )
    assert issue is not None
    assert issue.reason_code == "device.whatsapp_kind_invalid"


def _webhook_payload(*, message_id: str = "wamid.1") -> bytes:
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba-1",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "+15550001111",
                                    "phone_number_id": "phone-1",
                                },
                                "contacts": [
                                    {
                                        "profile": {"name": "Owner"},
                                        "wa_id": "15550002222",
                                    }
                                ],
                                "messages": [
                                    {
                                        "from": "15550002222",
                                        "id": message_id,
                                        "timestamp": "1789077600",
                                        "text": {"body": "Hello Veetbot"},
                                        "type": "text",
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        },
        separators=(",", ":"),
    ).encode()


def _signature(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SIGNATURE_KEY.encode(), body, hashlib.sha256).hexdigest()


async def test_webhook_handshake_requires_constant_time_verify_token() -> None:
    received: list[SurfaceInboundMessage] = []
    app = create_whatsapp_webhook_app(
        surface_id=SURFACE_ID,
        phone_number_id="phone-1",
        app_secret=SecretStr(APP_SIGNATURE_KEY),
        verify_token=SecretStr(VERIFICATION_CHALLENGE),
        ingest=received.append,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://surface.test"
    ) as client:
        denied = await client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "wrong",
                "hub.challenge": "challenge-value",
            },
        )
        accepted = await client.get(
            "/webhooks/whatsapp",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": VERIFICATION_CHALLENGE,
                "hub.challenge": "challenge-value",
            },
        )

    assert denied.status_code == 403
    assert denied.text == ""
    assert accepted.status_code == 200
    assert accepted.text == "challenge-value"
    assert received == []


async def test_webhook_rejects_bad_signature_before_parsing_or_ingest() -> None:
    received: list[SurfaceInboundMessage] = []
    app = create_whatsapp_webhook_app(
        surface_id=SURFACE_ID,
        phone_number_id="phone-1",
        app_secret=SecretStr(APP_SIGNATURE_KEY),
        verify_token=SecretStr(VERIFICATION_CHALLENGE),
        ingest=received.append,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://surface.test"
    ) as client:
        response = await client.post(
            "/webhooks/whatsapp",
            content=b"not-json-and-must-not-be-parsed",
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        )

    assert response.status_code == 401
    assert response.text == ""
    assert received == []


async def test_webhook_rejects_a_valid_signature_with_non_ascii_suffix() -> None:
    received: list[SurfaceInboundMessage] = []
    app = create_whatsapp_webhook_app(
        surface_id=SURFACE_ID,
        phone_number_id="phone-1",
        app_secret=SecretStr(APP_SIGNATURE_KEY),
        verify_token=SecretStr(VERIFICATION_CHALLENGE),
        ingest=received.append,
    )
    body = _webhook_payload()
    invalid_signature = (_signature(body) + "é").encode("utf-8")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://surface.test"
    ) as client:
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers=[(b"x-hub-signature-256", invalid_signature)],
        )

    assert response.status_code == 401
    assert response.text == ""
    assert received == []


async def test_signed_webhook_normalizes_text_and_preserves_meta_message_id() -> None:
    received: list[SurfaceInboundMessage] = []
    app = create_whatsapp_webhook_app(
        surface_id=SURFACE_ID,
        phone_number_id="phone-1",
        app_secret=SecretStr(APP_SIGNATURE_KEY),
        verify_token=SecretStr(VERIFICATION_CHALLENGE),
        ingest=received.append,
    )
    body = _webhook_payload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://surface.test"
    ) as client:
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": _signature(body)},
        )

    assert response.status_code == 200
    assert len(received) == 1
    update = received[0]
    assert update.surface_id == SURFACE_ID
    assert update.provider is PushProvider.WHATSAPP
    assert update.external_update_id == "wamid.1"
    assert update.sender_id == "15550002222"
    assert update.sender_label == "Owner"
    assert update.chat_ref == "15550002222"
    assert update.text == "Hello Veetbot"


async def test_webhook_bounds_body_and_confines_phone_number() -> None:
    received: list[SurfaceInboundMessage] = []
    app = create_whatsapp_webhook_app(
        surface_id=SURFACE_ID,
        phone_number_id="phone-1",
        app_secret=SecretStr(APP_SIGNATURE_KEY),
        verify_token=SecretStr(VERIFICATION_CHALLENGE),
        ingest=received.append,
        max_body_bytes=16,
    )
    body = _webhook_payload()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://surface.test"
    ) as client:
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": _signature(body)},
        )

    assert response.status_code == 413
    assert response.text == ""
    assert received == []


async def test_webhook_stops_streaming_an_undeclared_oversize_body() -> None:
    received: list[SurfaceInboundMessage] = []
    chunks_sent = 0

    async def oversized_body() -> AsyncIterator[bytes]:
        nonlocal chunks_sent
        for _ in range(20):
            chunks_sent += 1
            yield b"12345678"

    app = create_whatsapp_webhook_app(
        surface_id=SURFACE_ID,
        phone_number_id="phone-1",
        app_secret=SecretStr(APP_SIGNATURE_KEY),
        verify_token=SecretStr(VERIFICATION_CHALLENGE),
        ingest=received.append,
        max_body_bytes=16,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://surface.test"
    ) as client:
        response = await client.post(
            "/webhooks/whatsapp",
            content=oversized_body(),
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        )

    assert response.status_code == 413
    assert chunks_sent == 3
    assert received == []


async def test_cloud_transport_is_meta_origin_only_no_redirect_and_bounded() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.outbound"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = WhatsAppCloudTransport(
        access_token=SecretStr("access-token-value"),
        phone_number_id="phone-1",
        graph_api_version="v23.0",
        client=client,
    )
    try:
        result = await transport.send_text("15550002222", "Hello")
    finally:
        await client.aclose()

    assert result == SurfaceTransportResult(
        outcome=SurfaceTransportOutcome.DELIVERED,
        external_message_id="wamid.outbound",
    )
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url).startswith(f"{META_GRAPH_ORIGIN}/v23.0/phone-1/messages")
    assert request.headers["authorization"] == "Bearer access-token-value"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "15550002222",
        "type": "text",
        "text": {"preview_url": False, "body": "Hello"},
    }


class _CountingResponseStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.chunks_read = 0

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for _ in range(20):
            self.chunks_read += 1
            yield b"12345678"

    async def aclose(self) -> None:
        return


async def test_cloud_transport_stops_reading_when_response_bound_is_crossed() -> None:
    stream = _CountingResponseStream()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, stream=stream)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = WhatsAppCloudTransport(
        access_token=SecretStr("access-token-value"),
        phone_number_id="phone-1",
        graph_api_version="v23.0",
        client=client,
        max_response_bytes=16,
    )
    try:
        result = await transport.send_text("15550002222", "Hello")
    finally:
        await client.aclose()

    assert result.reason_code == "surface.response_too_large"
    assert stream.chunks_read == 3


async def test_cloud_transport_maps_malformed_success_response_to_closed_rejection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, content=b"\xff")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = WhatsAppCloudTransport(
        access_token=SecretStr("access-token-value"),
        phone_number_id="phone-1",
        graph_api_version="v23.0",
        client=client,
    )
    try:
        result = await transport.send_text("15550002222", "Hello")
    finally:
        await client.aclose()

    assert result == SurfaceTransportResult(
        outcome=SurfaceTransportOutcome.REJECTED,
        reason_code="surface.transport_invalid_response",
    )


class RecordingWhatsAppTransport:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []
        self.templates: list[str] = []

    async def send_text(self, chat_ref: str, text: str) -> SurfaceTransportResult:
        self.texts.append((chat_ref, text))
        return SurfaceTransportResult(outcome=SurfaceTransportOutcome.DELIVERED)

    async def send_update_template(self, chat_ref: str) -> SurfaceTransportResult:
        self.templates.append(chat_ref)
        return SurfaceTransportResult(outcome=SurfaceTransportOutcome.DELIVERED)


async def test_delivery_uses_freeform_inside_window_and_template_outside() -> None:
    transport = RecordingWhatsAppTransport()
    service = WhatsAppDeliveryService(transport=transport)

    inside = await service.deliver(
        "15550002222",
        "Inside",
        last_inbound_at=NOW - timedelta(hours=23, minutes=59),
        now=NOW,
    )
    outside = await service.deliver(
        "15550002222",
        "Must not leave",
        last_inbound_at=NOW - timedelta(hours=24, microseconds=1),
        now=NOW,
    )

    assert inside.outcome is SurfaceTransportOutcome.DELIVERED
    assert outside.outcome is SurfaceTransportOutcome.DELIVERED
    assert transport.texts == [("15550002222", "Inside")]
    assert transport.templates == ["15550002222"]


async def test_transport_errors_are_closed_and_never_echo_response_or_token() -> None:
    provider_body = "raw-provider-body-with-access-token-value"

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, text=provider_body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = WhatsAppCloudTransport(
        access_token=SecretStr("access-token-value"),
        phone_number_id="phone-1",
        graph_api_version="v23.0",
        client=client,
    )
    try:
        result = await transport.send_text("15550002222", "Hello")
    finally:
        await client.aclose()

    dumped: dict[str, Any] = result.model_dump(mode="json")
    assert dumped == {
        "outcome": "retry",
        "external_message_id": None,
        "reason_code": "surface.transport_unavailable",
    }
    assert "access-token-value" not in repr(result)
    assert provider_body not in repr(result)


def _change(
    phone_number_id: str, messages: list[dict[str, object]], field: str = "messages"
) -> dict[str, object]:
    return {
        "field": field,
        "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": phone_number_id},
            "messages": messages,
        },
    }


def _message(message_id: str, **content: object) -> dict[str, object]:
    return {"from": "15550002222", "id": message_id, "timestamp": "1789077600", **content}


async def _post_signed(document: object) -> tuple[int, list[SurfaceInboundMessage]]:
    received: list[SurfaceInboundMessage] = []
    app = create_whatsapp_webhook_app(
        surface_id=SURFACE_ID,
        phone_number_id="phone-1",
        app_secret=SecretStr(APP_SIGNATURE_KEY),
        verify_token=SecretStr(VERIFICATION_CHALLENGE),
        ingest=received.append,
    )
    body = json.dumps(document).encode()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://surface.test"
    ) as client:
        response = await client.post(
            "/webhooks/whatsapp",
            content=body,
            headers={"X-Hub-Signature-256": _signature(body)},
        )
    return response.status_code, received


async def test_signed_webhook_ingests_only_the_configured_number_and_marks_media() -> None:
    text = _message("wamid.own", type="text", text={"body": "Hello Veetbot"})
    status, received = await _post_signed(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        _change(
                            "phone-2", [_message("wamid.other", type="text", text={"body": "x"})]
                        ),
                        _change("phone-1", [text], field="statuses"),
                        _change(
                            "phone-1",
                            [text, _message("wamid.photo", type="image", image={"id": "media-1"})],
                        ),
                    ]
                }
            ],
        }
    )

    assert status == 200
    assert [update.external_update_id for update in received] == ["wamid.own", "wamid.photo"]
    own, photo = received
    assert (own.message_kind, own.text) == (SurfaceMessageKind.TEXT, "Hello Veetbot")
    assert (photo.message_kind, photo.text) == (SurfaceMessageKind.MEDIA, None)


@pytest.mark.parametrize(
    "document",
    [
        {"object": "page", "entry": []},
        {"object": "whatsapp_business_account", "entry": "not a list"},
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        _change("phone-1", [_message("wamid.1", type="text", text={"body": " "})])
                    ]
                }
            ],
        },
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "changes": [
                        _change("phone-1", [_message("wamid.1", timestamp="soon", type="text")])
                    ]
                }
            ],
        },
    ],
    ids=["other-object", "entries-not-a-list", "blank-text", "bad-timestamp"],
)
async def test_signed_but_malformed_webhook_is_refused_without_ingest(document: object) -> None:
    status, received = await _post_signed(document)
    assert status == 400
    assert received == []


async def test_a_redelivered_meta_message_id_is_processed_once_through_ingress() -> None:
    """The webhook's own normalization feeds the shared ingress; Meta's retry of
    the same message id finds the receipt and submits nothing twice."""

    from decimal import Decimal

    from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
    from agent_core.adapters.identity import ConfiguredSchedulePrincipalDirectory
    from agent_core.application.surfaces import PreparedSurfaceSubmission, SurfaceIngressService
    from agent_core.domain.surfaces import InboundDisposition, Pairing, SurfaceRunBudget
    from tests.contract.support import memory_uow_factory, principal, session

    _, uow_factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"run.write"}})
    async with uow_factory() as uow:
        await uow.surfaces.pairings.create_pairing(
            Pairing(
                id=UUID("00000000-0000-4000-8000-000000002510"),
                surface_id=SURFACE_ID,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                sender_id="15550002222",
                granted_scopes=frozenset({"run.write"}),
                paired_at=NOW,
            )
        )
    submitted: list[tuple[str, object]] = []

    async def create_session(uow, bound_principal, update):  # type: ignore[no-untyped-def]
        created = session().model_copy(
            update={
                "id": UUID("00000000-0000-4000-8000-000000002512"),
                "principal_id": bound_principal.principal_id,
                "created_at": NOW,
            }
        )
        await uow.sessions.create(created)
        return created

    async def submit(uow, bound_principal, mapped, text, origin, version, budget):  # type: ignore[no-untyped-def]
        del uow, bound_principal, mapped, version, budget
        submitted.append((text, origin["external_update_id"]))
        return PreparedSurfaceSubmission(
            run_id=UUID("00000000-0000-4000-8000-000000002511"),
            disposition=InboundDisposition.SUBMITTED,
            dispatch_kind="dispatch",
        )

    async def notice(chat_ref: str, reason_code: str) -> None:
        del chat_ref, reason_code

    ingress = SurfaceIngressService(
        uow_factory=uow_factory,
        principals=ConfiguredSchedulePrincipalDirectory(owner),
        run_budget=SurfaceRunBudget(max_cost=Decimal("10"), synthesis_reserve_cost=Decimal("2")),
        self_approval_enabled=True,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        create_session=create_session,
        submit=submit,
        dispatch=lambda prepared: None,
        notice=notice,
    )
    app = create_whatsapp_webhook_app(
        surface_id=SURFACE_ID,
        phone_number_id="phone-1",
        app_secret=SecretStr(APP_SIGNATURE_KEY),
        verify_token=SecretStr(VERIFICATION_CHALLENGE),
        ingest=ingress.ingest,
    )
    body = _webhook_payload(message_id="wamid.redelivered")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://surface.test"
    ) as client:
        for _ in range(2):
            response = await client.post(
                "/webhooks/whatsapp",
                content=body,
                headers={"X-Hub-Signature-256": _signature(body)},
            )
            assert response.status_code == 200

    assert submitted == [("Hello Veetbot", "wamid.redelivered")]
    async with uow_factory() as uow:
        receipt = await uow.surfaces.receipts.get(SURFACE_ID, "wamid.redelivered")
    assert receipt is not None
    assert receipt.disposition is InboundDisposition.SUBMITTED
