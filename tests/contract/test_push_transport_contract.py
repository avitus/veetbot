"""Shared push-transport contract exercised when transport adapters land."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import UUID

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pydantic import SecretStr

from agent_core.adapters.apns import APNsPushTransport
from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.push import FakePushTransport
from agent_core.domain.devices import PushEnvironment, PushProvider, PushTarget
from agent_core.domain.notifications import NotificationKind, NotificationPayload, PushMessage
from agent_core.ports.notifications import PushTransport
from tests.contract.support import NOW


async def assert_push_transport_returns_a_closed_outcome(
    transport: PushTransport,
    target: PushTarget,
    message: PushMessage,
) -> None:
    outcome = await transport.deliver(target, message)
    assert outcome.outcome.value in {
        "delivered",
        "retry",
        "unregistered",
        "rejected",
        "skipped",
    }


def push_target() -> PushTarget:
    return PushTarget(
        device_id=UUID(int=1201),
        provider=PushProvider.APNS,
        token=SecretStr("fake-device-token"),
        environment=PushEnvironment.SANDBOX,
        app_bundle_id="com.veetbot.app",
    )


def push_message() -> PushMessage:
    notification_id = UUID(int=1202)
    return PushMessage(
        notification_id=notification_id,
        dedupe_key="test:transport-contract",
        payload=NotificationPayload(
            kind=NotificationKind.TEST,
            title="Test notification",
            notification_id=notification_id,
        ),
        priority=5,
    )


async def test_fake_push_transport_satisfies_shared_contract() -> None:
    await assert_push_transport_returns_a_closed_outcome(
        FakePushTransport(), push_target(), push_message()
    )


async def test_apns_push_transport_satisfies_shared_contract(tmp_path: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    key_file = tmp_path / "AuthKey_CONTRACT.p8"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(key_file, 0o600)
    client = httpx.AsyncClient(
        base_url="https://api.sandbox.push.apple.com:443",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200)),
    )
    transport = APNsPushTransport(
        key_file=key_file,
        key_id="KEY123",
        team_id="TEAM123",
        topic="com.veetbot.app",
        clock=FixedClock(NOW),
        clients={PushEnvironment.SANDBOX: client},
    )

    await assert_push_transport_returns_a_closed_outcome(transport, push_target(), push_message())
    await transport.aclose()
