"""Milestone 14 operator API gates for surface pairing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
from pydantic import SecretStr

from agent_core.api import create_app
from agent_core.bootstrap import Composition, build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.devices import DeviceKind, DeviceRegistration, PushProvider
from agent_core.domain.surfaces import (
    SurfaceChatKind,
    SurfaceInboundMessage,
    SurfaceMessageKind,
)
from agent_core.policy.scopes import PLATFORM_SCOPES

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def _settings(tmp_path: Path, *, enabled: bool) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://unused/agent",
        deployment_mode=DeploymentMode.DEVELOPMENT,
        auth_mode=AuthMode.DEV,
        auth_token=None,
        sandbox=SandboxMechanism.FAKE,
        config_dir=None,
        credentials={},
        interpolation={"OPENAI_MODEL": ""},
        artifact_root=tmp_path / "artifacts",
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"user"}),
        auth_scopes=PLATFORM_SCOPES,
        surface_api_enabled=enabled,
        surface_worker_enabled=enabled,
    )


async def _client(composition: Composition, settings: Settings) -> httpx.AsyncClient:
    app = create_app(
        composition.services,
        settings,
        composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://agent.test",
    )


async def test_surface_routes_are_absent_by_default(tmp_path: Path) -> None:
    settings = _settings(tmp_path, enabled=False)
    async with (
        build(settings=settings, sequential_ids=True) as composition,
        await _client(composition, settings) as client,
    ):
        response = await client.get("/v1/surfaces")

    assert response.status_code == 404


async def test_pairing_code_is_single_presentation_and_lifecycle_is_scoped(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, enabled=True)
    async with build(settings=settings, sequential_ids=True) as composition:
        registered = await composition.services.devices.register(
            composition.principal,
            DeviceRegistration(
                client_device_id="whatsapp:15551234567",
                name="Veetbot WhatsApp",
                kind=DeviceKind.SURFACE,
                platform="whatsapp",
                push_provider=PushProvider.WHATSAPP,
                push_token=SecretStr("15551234567"),
            ),
        )
        surface_id = registered.device.id
        async with await _client(composition, settings) as client:
            listed = await client.get("/v1/surfaces")
            assert listed.status_code == 200
            assert [row["id"] for row in listed.json()] == [str(surface_id)]

            missing_key = await client.post(
                f"/v1/surfaces/{surface_id}/pairing-codes",
                json={"granted_scopes": ["run.write"], "label": "Owner"},
            )
            assert missing_key.status_code == 400

            issued = await client.post(
                f"/v1/surfaces/{surface_id}/pairing-codes",
                headers={"Idempotency-Key": "pair-owner"},
                json={"granted_scopes": ["run.write"], "label": "Owner"},
            )
            assert issued.status_code == 201, issued.text
            code = issued.json()["code"]
            assert code and code != "**********"
            assert "code_hash" not in issued.text
            assert "code_salt" not in issued.text

            replay = await client.post(
                f"/v1/surfaces/{surface_id}/pairing-codes",
                headers={"Idempotency-Key": "pair-owner"},
                json={"granted_scopes": ["run.write"], "label": "Owner"},
            )
            assert replay.status_code == 409
            assert code not in replay.text

            paired = await composition.surface_ingress.ingest(
                SurfaceInboundMessage(
                    surface_id=surface_id,
                    provider=PushProvider.WHATSAPP,
                    external_update_id="wamid.pair-owner",
                    sender_id="15550001111",
                    sender_label="Owner",
                    chat_ref="15550001111",
                    chat_kind=SurfaceChatKind.DIRECT,
                    message_kind=SurfaceMessageKind.TEXT,
                    text=f"/pair {code}",
                    received_at=NOW,
                )
            )
            assert paired.reason_code == "surface.paired"

            pairings = await client.get(f"/v1/surfaces/{surface_id}/pairings")
            assert pairings.status_code == 200
            pairing = pairings.json()[0]
            assert pairing["sender_id"] == "15550001111"
            assert code not in pairings.text

            revoked = await client.post(f"/v1/surfaces/pairings/{pairing['id']}/revoke")
            assert revoked.status_code == 200
            assert revoked.json()["revoked_at"] is not None

            rejected = await composition.surface_ingress.ingest(
                SurfaceInboundMessage(
                    surface_id=surface_id,
                    provider=PushProvider.WHATSAPP,
                    external_update_id="wamid.after-revoke",
                    sender_id="15550001111",
                    sender_label="Owner",
                    chat_ref="15550001111",
                    chat_kind=SurfaceChatKind.DIRECT,
                    message_kind=SurfaceMessageKind.TEXT,
                    text="must not create a run",
                    received_at=NOW,
                )
            )
            assert rejected.reason_code == "surface.unpaired"
            refreshed = await client.get(f"/v1/surfaces/{surface_id}")
            assert refreshed.status_code == 200
            assert refreshed.json()["push_token_fingerprint"] is None

            deleted = await client.delete(f"/v1/surfaces/pairings/{pairing['id']}")
            assert deleted.status_code == 204


async def test_surface_routes_hold_exact_scopes_absence_and_the_scope_ceiling(
    tmp_path: Path,
) -> None:
    """Reads need surface.read, writes surface.write; unknown ids are 404, never 500."""
    from uuid import UUID

    from agent_core.domain.agents import Principal

    settings = _settings(tmp_path, enabled=True)
    async with build(settings=settings, sequential_ids=True) as composition:
        registered = await composition.services.devices.register(
            composition.principal,
            DeviceRegistration(
                client_device_id="whatsapp:15551234567",
                name="Veetbot WhatsApp",
                kind=DeviceKind.SURFACE,
                platform="whatsapp",
                push_provider=PushProvider.WHATSAPP,
                push_token=SecretStr("15551234567"),
            ),
        )
        surface_id = registered.device.id
        unknown = UUID(int=404)

        def client_for(scopes: set[str]) -> httpx.AsyncClient:
            principal = Principal(
                tenant_id=composition.principal.tenant_id,
                principal_id=composition.principal.principal_id,
                roles=set(composition.principal.roles),
                scopes=scopes,
            )
            app = create_app(
                composition.services,
                settings,
                principal,
                composition.new_request_id,
                composition.readiness_probe,
            )
            return httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://agent.test",
            )

        mint = {"granted_scopes": ["run.write"], "label": "Owner"}
        async with client_for({"surface.read"}) as reader:
            assert (await reader.get("/v1/surfaces")).status_code == 200
            assert (await reader.get(f"/v1/surfaces/{surface_id}")).status_code == 200
            assert (await reader.get(f"/v1/surfaces/{unknown}")).status_code == 404
            assert (await reader.get(f"/v1/surfaces/{unknown}/pairings")).status_code == 404
            refused = await reader.post(
                f"/v1/surfaces/{surface_id}/pairing-codes",
                headers={"Idempotency-Key": "reader"},
                json=mint,
            )
            assert refused.status_code == 403
            assert (await reader.post(f"/v1/surfaces/pairings/{unknown}/revoke")).status_code == 403
            assert (await reader.delete(f"/v1/surfaces/pairings/{unknown}")).status_code == 403
        async with client_for({"surface.write", "run.write"}) as writer:
            assert (await writer.get("/v1/surfaces")).status_code == 403
            assert (await writer.get(f"/v1/surfaces/{surface_id}/pairings")).status_code == 403
            assert (await writer.post(f"/v1/surfaces/pairings/{unknown}/revoke")).status_code == 404
            assert (await writer.delete(f"/v1/surfaces/pairings/{unknown}")).status_code == 404
            missing_surface = await writer.post(
                f"/v1/surfaces/{unknown}/pairing-codes",
                headers={"Idempotency-Key": "unknown-surface"},
                json=mint,
            )
            assert missing_surface.status_code == 404
            # A code can grant only scopes its minter holds now (inbound-surfaces.md).
            widened = await writer.post(
                f"/v1/surfaces/{surface_id}/pairing-codes",
                headers={"Idempotency-Key": "widened"},
                json={**mint, "granted_scopes": ["run.write", "session.read"]},
            )
            assert widened.status_code == 409
            assert "code" not in widened.json()
