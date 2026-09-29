"""Milestone 14 operator API gates for surface pairing."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

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


async def _whatsapp_surface(composition: Composition) -> UUID:
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
    return registered.device.id


def _inbound(surface_id: UUID, update_id: str, text: str) -> SurfaceInboundMessage:
    return SurfaceInboundMessage(
        surface_id=surface_id,
        provider=PushProvider.WHATSAPP,
        external_update_id=update_id,
        sender_id="15550001111",
        sender_label="Owner",
        chat_ref="15550001111",
        chat_kind=SurfaceChatKind.DIRECT,
        message_kind=SurfaceMessageKind.TEXT,
        text=text,
        received_at=NOW,
    )


async def test_surface_responses_are_private_and_pairings_are_an_allow_listed_view(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, enabled=True)
    async with build(settings=settings, sequential_ids=True) as composition:
        surface_id = await _whatsapp_surface(composition)
        async with await _client(composition, settings) as client:
            issued = await client.post(
                f"/v1/surfaces/{surface_id}/pairing-codes",
                headers={"Idempotency-Key": "private-view"},
                json={"granted_scopes": ["run.write"], "label": "Owner"},
            )
            await composition.surface_ingress.ingest(
                _inbound(surface_id, "wamid.private-view", f"/pair {issued.json()['code']}")
            )
            listed = await client.get("/v1/surfaces")
            one = await client.get(f"/v1/surfaces/{surface_id}")
            pairings = await client.get(f"/v1/surfaces/{surface_id}/pairings")
            revoked = await client.post(f"/v1/surfaces/pairings/{pairings.json()[0]['id']}/revoke")

    public_fields = {
        "id",
        "surface_id",
        "sender_id",
        "sender_label",
        "granted_scopes",
        "paired_at",
        "revoked_at",
        "last_message_at",
    }
    for response in (issued, listed, one, pairings, revoked):
        assert response.status_code in {200, 201}, response.text
        assert response.headers["Cache-Control"] == "private, no-store"
    assert set(pairings.json()[0]) == public_fields
    assert set(revoked.json()) == public_fields
    assert composition.principal.tenant_id not in pairings.text


async def test_a_repeated_pairing_key_is_a_non_retryable_conflict_naming_the_code(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, enabled=True)
    async with build(settings=settings, sequential_ids=True) as composition:
        surface_id = await _whatsapp_surface(composition)
        async with await _client(composition, settings) as client:
            path = f"/v1/surfaces/{surface_id}/pairing-codes"
            headers = {"Idempotency-Key": "lost-response"}
            body = {"granted_scopes": ["run.write"], "label": "Owner"}
            issued = await client.post(path, headers=headers, json=body)
            replay = await client.post(path, headers=headers, json=body)

    assert issued.status_code == 201
    assert replay.status_code == 409
    details = replay.json()["error"]["details"]
    assert details == {
        "reason": "surface.pairing_code_already_issued",
        "pairing_code_id": issued.json()["id"],
        "expires_at": issued.json()["expires_at"],
    }
    assert issued.json()["code"] not in replay.text


async def test_composed_surface_ingress_refuses_self_approval_when_the_profile_does(
    tmp_path: Path,
) -> None:
    from dataclasses import replace
    from uuid import UUID as _UUID

    from agent_core.domain.approvals import ApprovalRequest, ApprovalStatus
    from agent_core.domain.policies import (
        ActionKind,
        PolicyDecision,
        PolicyDecisionType,
        RiskLevel,
    )
    from agent_core.domain.runs import RunStatus
    from agent_core.domain.surfaces import Pairing
    from tests.contract.support import run, session

    config_dir = tmp_path / "config"
    overlay = config_dir / "policy" / "default.yaml"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("self_approval:\n  enabled: false\n", encoding="utf-8")
    settings = replace(_settings(tmp_path, enabled=True), config_dir=config_dir)
    approval_id = _UUID("00000000-0000-4000-8000-0000000014d0")
    async with build(settings=settings, sequential_ids=True) as composition:
        surface_id = await _whatsapp_surface(composition)
        owner = composition.principal
        waiting = run(status=RunStatus.WAITING_FOR_APPROVAL).model_copy(
            update={
                "id": _UUID("00000000-0000-4000-8000-0000000014d3"),
                "session_id": _UUID("00000000-0000-4000-8000-0000000014d2"),
                "tenant_id": owner.tenant_id,
                "principal_scopes": {"approval.resolve"},
            }
        )
        async with composition.uow_factory() as uow:
            await uow.sessions.create(
                session().model_copy(
                    update={
                        "id": waiting.session_id,
                        "tenant_id": owner.tenant_id,
                        "principal_id": owner.principal_id,
                    }
                )
            )
            await uow.runs.create(waiting)
            await uow.surfaces.pairings.create_pairing(
                Pairing(
                    id=_UUID("00000000-0000-4000-8000-0000000014d1"),
                    surface_id=surface_id,
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    sender_id="15550001111",
                    granted_scopes=frozenset({"approval.resolve"}),
                    paired_at=NOW,
                )
            )
            await uow.approvals.create(
                ApprovalRequest(
                    id=approval_id,
                    tenant_id=owner.tenant_id,
                    principal_id=owner.principal_id,
                    session_id=_UUID("00000000-0000-4000-8000-0000000014d2"),
                    run_id=_UUID("00000000-0000-4000-8000-0000000014d3"),
                    action_kind=ActionKind.TOOL_CALL,
                    action_id=_UUID("00000000-0000-4000-8000-0000000014d4"),
                    status=ApprovalStatus.PENDING,
                    action_summary="Send a message.",
                    arguments={},
                    normalized_arguments_hash="hash",
                    required_scopes=set(),
                    agent_version="1.0.0",
                    risk=RiskLevel.HIGH,
                    policy_reason="policy.test",
                    policy_decision=PolicyDecision(
                        decision=PolicyDecisionType.REQUIRE_APPROVAL,
                        reason_code="policy.test",
                        explanation="Test approval.",
                        policy_version="test@1",
                    ),
                    policy_version="test@1",
                    created_at=NOW,
                )
            )

        refused = await composition.surface_ingress.ingest(
            _inbound(surface_id, "wamid.self-approve", f"/approve {approval_id}")
        )
        async with composition.uow_factory() as uow:
            approval = await uow.approvals.get(approval_id, owner)

    assert refused.reason_code == "surface.self_approval_denied"
    assert approval.status is ApprovalStatus.PENDING
