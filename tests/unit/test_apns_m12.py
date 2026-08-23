"""APNs authentication, addressing, and outcome mapping."""

from __future__ import annotations

import base64
import json
import os
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from agent_core.adapters.apns import APNsPushTransport
from agent_core.adapters.determinism import FixedClock
from agent_core.domain.devices import PushEnvironment
from agent_core.domain.notifications import DeliveryOutcome
from tests.contract.support import NOW
from tests.contract.test_push_transport_contract import push_message, push_target


def _private_key_file(tmp_path: Path) -> tuple[Path, ec.EllipticCurvePrivateKey]:
    key = ec.generate_private_key(ec.SECP256R1())
    path = tmp_path / "AuthKey_TEST.p8"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(path, 0o600)
    return path, key


def _decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


async def test_apns_signs_addresses_and_refreshes_provider_token(
    tmp_path: Path,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"apns-id": f"apns-{len(requests)}"})

    key_path, key = _private_key_file(tmp_path)
    clients = {
        environment: httpx.AsyncClient(
            base_url=host,
            transport=httpx.MockTransport(handler),
        )
        for environment, host in {
            PushEnvironment.SANDBOX: "https://api.sandbox.push.apple.com:443",
            PushEnvironment.PRODUCTION: "https://api.push.apple.com:443",
        }.items()
    }
    clock = FixedClock(NOW)
    transport = APNsPushTransport(
        key_file=key_path,
        key_id="KEY123",
        team_id="TEAM123",
        topic="com.veetbot.app",
        clock=clock,
        clients=clients,
    )
    message = push_message().model_copy(update={"expires_at": NOW + timedelta(hours=1)})

    first = await transport.deliver(push_target(), message)
    production_target = push_target().model_copy(update={"environment": PushEnvironment.PRODUCTION})
    second = await transport.deliver(production_target, message)
    clock.advance(timedelta(minutes=20))
    third = await transport.deliver(push_target(), message)

    assert [first.outcome, second.outcome, third.outcome] == [DeliveryOutcome.DELIVERED] * 3
    assert [request.url.host for request in requests] == [
        "api.sandbox.push.apple.com",
        "api.push.apple.com",
        "api.sandbox.push.apple.com",
    ]
    assert all(request.url.path == "/3/device/fake-device-token" for request in requests)
    first_headers = requests[0].headers
    assert first_headers["apns-topic"] == "com.veetbot.app"
    assert first_headers["apns-push-type"] == "alert"
    assert first_headers["apns-priority"] == "5"
    assert first_headers["apns-collapse-id"] == "test:transport-contract"
    assert message.expires_at is not None
    assert first_headers["apns-expiration"] == str(int(message.expires_at.timestamp()))
    assert json.loads(requests[0].content) == {
        "aps": {"alert": {"title": "Test notification"}},
        "veetbot": message.payload.model_dump(mode="json"),
    }

    tokens = [request.headers["authorization"].split(" ", 1)[1] for request in requests]
    assert tokens[0] == tokens[1]
    assert tokens[2] != tokens[0]
    encoded_header, encoded_claims, encoded_signature = tokens[0].split(".")
    assert json.loads(_decode(encoded_header)) == {"alg": "ES256", "kid": "KEY123"}
    assert json.loads(_decode(encoded_claims)) == {
        "iss": "TEAM123",
        "iat": int(NOW.timestamp()),
    }
    signature = _decode(encoded_signature)
    assert len(signature) == 64
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    key.public_key().verify(
        encode_dss_signature(r, s),
        f"{encoded_header}.{encoded_claims}".encode("ascii"),
        ec.ECDSA(hashes.SHA256()),
    )
    assert "fake-device-token" not in repr(transport)
    assert key_path.read_text(encoding="ascii") not in repr(transport)
    await transport.aclose()


@pytest.mark.parametrize(
    ("dedupe_key", "expected"),
    [
        ("x" * 64, "x" * 64),
        ("x" * 65, None),
        ("push:✅" * 20, None),
    ],
)
async def test_apns_collapse_identifier_is_bounded_to_64_utf8_bytes(
    tmp_path: Path,
    dedupe_key: str,
    expected: str | None,
) -> None:
    requests: list[httpx.Request] = []
    key_path, _key = _private_key_file(tmp_path)

    def record(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    client = httpx.AsyncClient(
        base_url="https://api.sandbox.push.apple.com:443",
        transport=httpx.MockTransport(record),
    )
    transport = APNsPushTransport(
        key_file=key_path,
        key_id="KEY123",
        team_id="TEAM123",
        topic="com.veetbot.app",
        clock=FixedClock(NOW),
        clients={PushEnvironment.SANDBOX: client},
    )

    await transport.deliver(
        push_target(),
        push_message().model_copy(update={"dedupe_key": dedupe_key}),
    )

    collapse_id = requests[0].headers["apns-collapse-id"]
    assert len(collapse_id.encode("utf-8")) <= 64
    if expected is not None:
        assert collapse_id == expected
    else:
        assert collapse_id != dedupe_key
        assert len(collapse_id) == 64
    await transport.aclose()


def test_apns_owned_clients_have_explicit_timeouts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key_path, _key = _private_key_file(tmp_path)
    created: list[dict[str, object]] = []

    class Client:
        def __init__(self, **kwargs: object) -> None:
            created.append(kwargs)

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(httpx, "AsyncClient", Client)

    APNsPushTransport(
        key_file=key_path,
        key_id="KEY123",
        team_id="TEAM123",
        topic="com.veetbot.app",
        clock=FixedClock(NOW),
    )

    assert len(created) == 2
    assert all(call["http2"] is True for call in created)
    assert all(call["timeout"] == httpx.Timeout(10.0, connect=5.0) for call in created)


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        (410, "Unregistered", DeliveryOutcome.UNREGISTERED),
        (400, "BadDeviceToken", DeliveryOutcome.UNREGISTERED),
        (400, "DeviceTokenNotForTopic", DeliveryOutcome.UNREGISTERED),
        (429, "TooManyRequests", DeliveryOutcome.RETRY),
        (500, "InternalServerError", DeliveryOutcome.RETRY),
        (403, "ExpiredProviderToken", DeliveryOutcome.RETRY),
        (400, "PayloadEmpty", DeliveryOutcome.REJECTED),
    ],
)
async def test_apns_maps_provider_outcomes(
    tmp_path: Path,
    status: int,
    reason: str,
    expected: DeliveryOutcome,
) -> None:
    key_path, _key = _private_key_file(tmp_path)
    client = httpx.AsyncClient(
        base_url="https://api.sandbox.push.apple.com:443",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                status,
                json={"reason": reason},
                headers={"apns-id": "provider-id"},
            )
        ),
    )
    transport = APNsPushTransport(
        key_file=key_path,
        key_id="KEY123",
        team_id="TEAM123",
        topic="com.veetbot.app",
        clock=FixedClock(NOW),
        clients={PushEnvironment.SANDBOX: client},
    )

    outcome = await transport.deliver(push_target(), push_message())

    assert outcome.outcome is expected
    assert outcome.provider_reason == reason
    assert outcome.provider_id == "provider-id"
    await transport.aclose()


async def test_apns_network_failure_retries_without_exposing_credentials(tmp_path: Path) -> None:
    key_path, _key = _private_key_file(tmp_path)

    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection unavailable", request=request)

    client = httpx.AsyncClient(
        base_url="https://api.sandbox.push.apple.com:443",
        transport=httpx.MockTransport(fail),
    )
    transport = APNsPushTransport(
        key_file=key_path,
        key_id="KEY123",
        team_id="TEAM123",
        topic="com.veetbot.app",
        clock=FixedClock(NOW),
        clients={PushEnvironment.SANDBOX: client},
    )

    outcome = await transport.deliver(push_target(), push_message())

    assert outcome.outcome is DeliveryOutcome.RETRY
    assert outcome.provider_reason == "NetworkError"
    assert "fake-device-token" not in repr(outcome)
    assert key_path.read_text(encoding="ascii") not in repr(transport)
    await transport.aclose()


async def test_expired_provider_token_is_discarded_before_next_attempt(tmp_path: Path) -> None:
    key_path, _key = _private_key_file(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(403, json={"reason": "ExpiredProviderToken"})
        return httpx.Response(200)

    client = httpx.AsyncClient(
        base_url="https://api.sandbox.push.apple.com:443",
        transport=httpx.MockTransport(handler),
    )
    transport = APNsPushTransport(
        key_file=key_path,
        key_id="KEY123",
        team_id="TEAM123",
        topic="com.veetbot.app",
        clock=FixedClock(NOW),
        clients={PushEnvironment.SANDBOX: client},
    )

    first = await transport.deliver(push_target(), push_message())
    second = await transport.deliver(push_target(), push_message())

    assert first.outcome is DeliveryOutcome.RETRY
    assert second.outcome is DeliveryOutcome.DELIVERED
    assert requests[0].headers["authorization"] != requests[1].headers["authorization"]
    await transport.aclose()


def test_apns_rejects_non_private_key_file(tmp_path: Path) -> None:
    key_path, _key = _private_key_file(tmp_path)
    os.chmod(key_path, 0o644)

    with pytest.raises(ValueError, match="mode 0600"):
        APNsPushTransport(
            key_file=key_path,
            key_id="KEY123",
            team_id="TEAM123",
            topic="com.veetbot.app",
            clock=FixedClock(NOW),
        )
