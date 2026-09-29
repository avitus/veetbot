"""Golden journey: the owner works from Telegram, approval included (Milestone 14).

The surface gates prove ingress, the approval command, the question prompt, and
the reply outbox one at a time against hand-seeded in-memory rows. This joins
them on PostgreSQL around the durable worker: the owner pairs a chat with a code
issued over HTTP, asks for an external write in Telegram, receives the approval
prompt there, answers with the command the prompt names, and receives the final
reply in the same chat. Updates enter through the composition's
`SurfaceIngressService` in place of the Telegram poller; the outbound halves are
the classes the surface role composes (`build_surface_worker`), wired to a
recording Telegram delivery instead of the Bot API.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx

from agent_core.adapters.determinism import FixedClock, SystemClock
from agent_core.adapters.identity import ConfiguredSchedulePrincipalDirectory
from agent_core.api import create_app
from agent_core.application.notification_dispatcher import NotificationDispatcher
from agent_core.application.surface_worker import (
    SurfaceNotificationTransport,
    SurfaceReplyDispatcher,
)
from agent_core.application.trajectory_service import TrajectoryRedactor
from agent_core.bootstrap import Composition, build
from agent_core.domain.devices import Device, DeviceKind, DeviceStatus, PushProvider
from agent_core.domain.messages import FakeModelScript, ScriptedToolCall, ScriptedTurn, StopReason
from agent_core.domain.runs import RunStatus
from agent_core.domain.surfaces import (
    InboundDisposition,
    SurfaceChatKind,
    SurfaceInboundMessage,
    SurfaceMessageKind,
    SurfaceTransportOutcome,
    SurfaceTransportResult,
)
from agent_core.policy.scopes import PLATFORM_SCOPES
from agent_core.runtime.worker import DurableWorker
from tests.integration.m2_support import database_settings

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)
CHAT = "424242"


@asynccontextmanager
async def _client(composition: Composition) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(
        composition.services,
        composition.settings,
        composition.principal,
        composition.new_request_id,
        composition.readiness_probe,
    )
    transport = httpx.ASGITransport(
        app=app, client=("127.0.0.1", 43107), raise_app_exceptions=False
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://agent.test") as client:
        yield client


class _TelegramChat:
    """Records what the bot sends to the owner's chat."""

    def __init__(self) -> None:
        self.sent: list[str] = []

    async def deliver(
        self,
        chat_ref: str,
        text: str,
        *,
        last_inbound_at: datetime | None,
        now: datetime,
    ) -> SurfaceTransportResult:
        del last_inbound_at, now
        assert chat_ref == CHAT
        self.sent.append(text)
        return SurfaceTransportResult(outcome=SurfaceTransportOutcome.DELIVERED)


async def _register_telegram_surface(composition: Composition) -> UUID:
    """Register the configured bot the way the surface role does at start."""

    principal = composition.principal
    now = composition.clock.now()
    surface = Device(
        id=uuid5(NAMESPACE_URL, f"veetbot:surface:{principal.tenant_id}:telegram:configured"),
        tenant_id=principal.tenant_id,
        principal_id=principal.principal_id,
        client_device_id="telegram:configured",
        name="Veetbot Telegram",
        kind=DeviceKind.SURFACE,
        platform=PushProvider.TELEGRAM.value,
        muted_kinds=frozenset(),
        capabilities=frozenset(),
        status=DeviceStatus.ACTIVE,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    async with composition.uow_factory() as uow:
        stored = await uow.devices.upsert(surface, principal)
    return stored.id


def _update(surface_id: UUID, update_id: int, text: str) -> SurfaceInboundMessage:
    return SurfaceInboundMessage(
        surface_id=surface_id,
        provider=PushProvider.TELEGRAM,
        external_update_id=str(update_id),
        sender_id=CHAT,
        chat_ref=CHAT,
        chat_kind=SurfaceChatKind.DIRECT,
        message_kind=SurfaceMessageKind.TEXT,
        text=text,
        received_at=NOW + timedelta(seconds=update_id),
    )


async def _work(composition: Composition, worker_id: str) -> None:
    worker = DurableWorker(
        uow_factory=composition.uow_factory,
        executor=composition.executor,
        clock=SystemClock(),
        worker_id=worker_id,
    )
    assert await worker.run_once()


async def test_a_paired_telegram_chat_asks_approves_and_receives_the_reply() -> None:
    reply = "Recorded the note in the demo log."
    script = FakeModelScript(
        turns=[
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="demo.external_write",
                        arguments={"destination": "demo", "content": "telegram-approved"},
                        call_id="golden-surface-write",
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            ),
            ScriptedTurn(text=reply),
        ]
    )
    settings = replace(
        database_settings(),
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"user"}),
        auth_scopes=PLATFORM_SCOPES,
        surface_api_enabled=True,
        surface_worker_enabled=True,
        notification_api_enabled=True,
        notification_dispatch_enabled=True,
    )
    chat = _TelegramChat()
    clock = FixedClock(NOW)
    async with (
        build(
            settings=settings,
            storage="postgres",
            script=script,
            clock=clock,
        ) as composition,
        _client(composition) as client,
    ):
        surface_id = await _register_telegram_surface(composition)
        issued = await client.post(
            f"/v1/surfaces/{surface_id}/pairing-codes",
            headers={"Idempotency-Key": "golden-telegram-pairing"},
            json={
                "granted_scopes": [
                    "run.read",
                    "run.write",
                    "approval.read",
                    "approval.resolve",
                    "demo.write",
                ],
                "label": "Owner phone",
            },
        )
        assert issued.status_code == 201, issued.text
        ingress = composition.surface_ingress
        paired = await ingress.ingest(_update(surface_id, 1, f"/pair {issued.json()['code']}"))
        assert paired.reason_code == "surface.paired", paired

        asked = await ingress.ingest(_update(surface_id, 2, "Please note telegram-approved."))
        assert asked.disposition is InboundDisposition.SUBMITTED, asked
        assert asked.run_id is not None
        run_id = asked.run_id

        await _work(composition, "golden-surface-proposer")
        parked = await client.get(f"/v1/runs/{run_id}")
        assert parked.json()["status"] == RunStatus.WAITING_FOR_APPROVAL.value, parked.json()
        [approval] = (await client.get("/v1/approvals", params={"run_id": str(run_id)})).json()[
            "items"
        ]

        redactor = TrajectoryRedactor()
        prompts = NotificationDispatcher(
            uow_factory=composition.uow_factory,
            transport=SurfaceNotificationTransport(
                uow_factory=composition.uow_factory,
                principals=ConfiguredSchedulePrincipalDirectory(composition.principal),
                deliveries={PushProvider.TELEGRAM: chat.deliver},
                template_policies=None,
                clock=clock,
                redactor=redactor,
            ),
            providers=frozenset({PushProvider.TELEGRAM}),
            clock=clock,
            ids=composition.ids,
            claimant="golden-surface-notify",
            batch_size=10,
            lease_seconds=30,
            retry_delays=(30, 120),
        )
        replies = SurfaceReplyDispatcher(
            uow_factory=composition.uow_factory,
            deliveries={PushProvider.TELEGRAM: chat.deliver},
            clock=clock,
            redactor=redactor,
            worker_id="golden-surface-replies",
            batch_size=10,
            lease_seconds=30,
            retry_delays=(30,),
            max_attempts=3,
        )
        assert await prompts.run_once() == 1
        [prompt] = chat.sent
        # The owner types the command the prompt offers: "/approve <id> or /deny <id>".
        [offer] = [line for line in prompt.splitlines() if line.startswith("/approve ")]
        command = " ".join(offer.split()[:2])

        approved = await ingress.ingest(_update(surface_id, 3, command))
        assert approved.reason_code == "surface.approval_resolved", approved
        await _work(composition, "golden-surface-resumer")
        completed = await client.get(f"/v1/runs/{run_id}")
        assert completed.json()["status"] == RunStatus.COMPLETED.value, completed.json()

        assert await replies.run_once() == 1
        assert await replies.run_once() == 0
        async with composition.uow_factory() as uow:
            invocations = await uow.invocations.list_for_run(run_id, composition.principal)
        resolved = await client.get(f"/v1/approvals/{approval['id']}")
        sessions = await client.get("/v1/sessions")

    assert prompt.startswith("Approval needed\n")
    assert f"ID: {approval['id'].split('-', 1)[0]}" in prompt
    assert resolved.json()["decision"] == "approve_once", resolved.json()
    assert chat.sent[1:] == [reply]
    assert [invocation.tool_name for invocation in invocations] == ["demo.external_write"]
    assert invocations[0].structured_result is not None
    # The chat the owner opened from Telegram is one ordinary session.
    assert sessions.status_code == 200, sessions.text
    assert [item["id"] for item in sessions.json()["items"]] == [
        str(completed.json()["session_id"])
    ]


async def test_the_shipped_default_agent_admits_a_paired_telegram_message() -> None:
    """The shipped limits give a surface run its own budget (ADR-0064 amendment):
    PostgreSQL admission reserves it, the run carries it, and a run the owner
    starts from the Apple client keeps the ordinary uncapped chat limits."""

    settings = replace(
        database_settings(),
        auth_tenant_id="local",
        auth_principal_id="local-user",
        auth_roles=frozenset({"user"}),
        auth_scopes=PLATFORM_SCOPES,
        surface_api_enabled=True,
        surface_worker_enabled=True,
    )
    async with (
        build(settings=settings, storage="postgres", fixed_clock_at=NOW) as composition,
        _client(composition) as client,
    ):
        surface_id = await _register_telegram_surface(composition)
        issued = await client.post(
            f"/v1/surfaces/{surface_id}/pairing-codes",
            headers={"Idempotency-Key": "golden-telegram-default-limits"},
            json={"granted_scopes": ["run.read", "run.write"]},
        )
        assert issued.status_code == 201, issued.text
        ingress = composition.surface_ingress
        paired = await ingress.ingest(_update(surface_id, 1, f"/pair {issued.json()['code']}"))
        assert paired.reason_code == "surface.paired", paired
        asked = await ingress.ingest(_update(surface_id, 2, "What time is it?"))
        assert asked.disposition is InboundDisposition.SUBMITTED, asked
        assert asked.run_id is not None
        created = await client.post("/v1/sessions", json={"agent_id": "general", "metadata": {}})
        assert created.status_code == 201, created.text
        posted = await client.post(
            f"/v1/sessions/{created.json()['id']}/messages",
            json={"content": [{"type": "text", "text": "What time is it?"}]},
        )
        assert posted.status_code == 202, posted.text
        apple_view = await client.get(f"/v1/runs/{posted.json()['run_id']}")
        surface_view = await client.get(f"/v1/runs/{asked.run_id}")
        async with composition.uow_factory() as uow:
            surface_run = await uow.runs.get(asked.run_id, composition.principal)

    assert surface_run.limits.max_cost == Decimal("10")
    assert surface_run.limits.synthesis_reserve_cost == Decimal("2")
    assert surface_view.json()["limits"]["max_cost_usd"] == "10"
    assert apple_view.json()["limits"]["max_cost_usd"] is None
