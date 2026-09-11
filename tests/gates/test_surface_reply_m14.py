"""Milestone 14 terminal reply outbox gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from pydantic import SecretStr

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.identity import ConfiguredSchedulePrincipalDirectory
from agent_core.adapters.whatsapp import WhatsAppDeliveryService
from agent_core.application.surface_worker import (
    SurfaceNotificationTransport,
    SurfaceReplyDispatcher,
)
from agent_core.application.trajectory_service import TrajectoryRedactor
from agent_core.bootstrap import build
from agent_core.config import AuthMode, DeploymentMode, SandboxMechanism, Settings
from agent_core.domain.devices import (
    Device,
    DeviceKind,
    DeviceRegistration,
    DeviceStatus,
    PushProvider,
    PushTarget,
)
from agent_core.domain.messages import FakeModelScript, ScriptedTurn
from agent_core.domain.notifications import (
    DeliveryOutcome,
    NotificationKind,
    NotificationPayload,
    PushMessage,
)
from agent_core.domain.runs import RunCheckpoint, RunStatus
from agent_core.domain.surfaces import (
    InboundDisposition,
    Pairing,
    SurfaceChatKind,
    SurfaceInboundMessage,
    SurfaceMessageKind,
    SurfaceTransportOutcome,
    SurfaceTransportResult,
)
from agent_core.policy.scopes import PLATFORM_SCOPES
from tests.contract.support import memory_uow_factory, principal, run

NOW = datetime(2026, 9, 11, 15, 0, tzinfo=UTC)


def _message(surface_id: UUID, update_id: str, text: str) -> SurfaceInboundMessage:
    return SurfaceInboundMessage(
        surface_id=surface_id,
        provider=PushProvider.WHATSAPP,
        external_update_id=update_id,
        sender_id="15550001111",
        chat_ref="15550001111",
        chat_kind=SurfaceChatKind.DIRECT,
        message_kind=SurfaceMessageKind.TEXT,
        text=text,
        received_at=NOW,
    )


async def test_terminal_surface_run_enqueues_then_sends_redacted_chunks_once(
    tmp_path: Path,
) -> None:
    settings = Settings(
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
        surface_api_enabled=True,
        surface_worker_enabled=True,
    )
    async with build(
        settings=settings,
        clock=FixedClock(NOW),
        sequential_ids=True,
        script=FakeModelScript(turns=[ScriptedTurn(text=("A" * 4100) + " secretword")]),
    ) as composition:
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
        issued = await composition.services.surfaces.issue_code(
            composition.principal,
            registered.device.id,
            granted_scopes=frozenset({"run.write", "run.read"}),
            label="Owner",
            idempotency_key="terminal-reply",
        )
        await composition.surface_ingress.ingest(
            _message(
                registered.device.id,
                "wamid.pair",
                f"/pair {issued.code.get_secret_value()}",
            )
        )

        result = await composition.surface_ingress.ingest(
            _message(registered.device.id, "wamid.ask", "Answer in WhatsApp")
        )
        assert result.disposition is InboundDisposition.SUBMITTED
        assert result.run_id is not None
        async with composition.uow_factory() as uow:
            submitted_run = await uow.runs.get(result.run_id, composition.principal)
            events = await uow.events.list_after(
                submitted_run.session_id,
                0,
                composition.principal,
            )
            user_events = [event for event in events if event.event_type == "user.message.created"]
            checkpoints = await uow.checkpoints.latest(result.run_id)
            sessions = await uow.sessions.list(composition.principal, limit=10)
            replies = await uow.surfaces.replies.claim_due(
                NOW,
                limit=10,
                worker_id="surface:test",
                lease_seconds=30,
            )
            await uow.surfaces.replies.retry(
                replies[0].id,
                worker_id="surface:test",
                next_attempt_at=NOW,
            )

        assert submitted_run.priority == 0
        assert len(user_events) == 1
        assert submitted_run.seed_event_sequence == user_events[0].sequence
        assert user_events[0].actor_type == "surface"
        assert user_events[0].payload["content"] == [{"kind": "text", "text": "Answer in WhatsApp"}]
        assert checkpoints is not None
        assert checkpoints.run_id == result.run_id
        assert len(sessions) == 1

        sent: list[str] = []

        async def deliver(
            chat_ref: str,
            text: str,
            *,
            last_inbound_at: datetime | None,
            now: datetime,
        ) -> SurfaceTransportResult:
            assert chat_ref == "15550001111"
            assert last_inbound_at == NOW
            assert now == NOW
            sent.append(text)
            return SurfaceTransportResult(outcome=SurfaceTransportOutcome.DELIVERED)

        dispatcher = SurfaceReplyDispatcher(
            uow_factory=composition.uow_factory,
            deliveries={PushProvider.WHATSAPP: deliver},
            clock=composition.clock,
            redactor=TrajectoryRedactor((("tenant", "secretword"),)),
            worker_id="surface:dispatch",
            batch_size=10,
            lease_seconds=30,
            retry_delays=(30,),
            chunk_size=4096,
        )
        assert await dispatcher.run_once() == 1
        assert await dispatcher.run_once() == 0

    assert len(replies) == 1
    assert replies[0].run_id == result.run_id
    assert replies[0].chat_ref == "15550001111"
    assert "secretword" not in replies[0].model_dump_json()
    assert len(sent) == 2
    assert all(len(chunk) <= 4096 for chunk in sent)
    assert "secretword" not in "".join(sent)
    assert "[redacted:tenant]" in "".join(sent)


class _RecordingWhatsAppTransport:
    def __init__(self) -> None:
        self.texts: list[str] = []
        self.templates: list[str] = []

    async def send_text(self, chat_ref: str, text: str) -> SurfaceTransportResult:
        del chat_ref
        self.texts.append(text)
        return SurfaceTransportResult(outcome=SurfaceTransportOutcome.DELIVERED)

    async def send_update_template(self, chat_ref: str) -> SurfaceTransportResult:
        self.templates.append(chat_ref)
        return SurfaceTransportResult(outcome=SurfaceTransportOutcome.DELIVERED)


async def test_whatsapp_reply_outside_window_sends_one_content_free_template(
    tmp_path: Path,
) -> None:
    clock = FixedClock(NOW)
    settings = Settings(
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
        surface_api_enabled=True,
        surface_worker_enabled=True,
    )
    async with build(
        settings=settings,
        clock=clock,
        sequential_ids=True,
        script=FakeModelScript(turns=[ScriptedTurn(text="B" * 5000)]),
    ) as composition:
        registered = await composition.services.devices.register(
            composition.principal,
            DeviceRegistration(
                client_device_id="whatsapp:window-test",
                name="Veetbot WhatsApp",
                kind=DeviceKind.SURFACE,
                platform="whatsapp",
                push_provider=PushProvider.WHATSAPP,
                push_token=SecretStr("15551234567"),
            ),
        )
        issued = await composition.services.surfaces.issue_code(
            composition.principal,
            registered.device.id,
            granted_scopes=frozenset({"run.write", "run.read"}),
            label="Owner",
            idempotency_key="outside-window",
        )
        await composition.surface_ingress.ingest(
            _message(
                registered.device.id,
                "wamid.window.pair",
                f"/pair {issued.code.get_secret_value()}",
            )
        )
        result = await composition.surface_ingress.ingest(
            _message(registered.device.id, "wamid.window.ask", "Answer later")
        )
        assert result.disposition is InboundDisposition.SUBMITTED

        clock.advance(timedelta(hours=24, microseconds=1))
        transport = _RecordingWhatsAppTransport()
        policy = WhatsAppDeliveryService(transport=transport)
        dispatcher = SurfaceReplyDispatcher(
            uow_factory=composition.uow_factory,
            deliveries={PushProvider.WHATSAPP: policy.deliver},
            template_policies={PushProvider.WHATSAPP: policy},
            clock=clock,
            redactor=TrajectoryRedactor(),
            worker_id="surface:outside-window",
            batch_size=10,
            lease_seconds=30,
            retry_delays=(30,),
            chunk_size=4096,
        )

        assert await dispatcher.run_once() == 1
        assert await dispatcher.run_once() == 0

    assert transport.texts == []
    assert transport.templates == ["15550001111"]


async def test_question_notification_reads_redacts_and_delivers_paired_detail() -> None:
    _, uow_factory = await memory_uow_factory()
    owner = principal().model_copy(update={"scopes": {"run.read"}})
    surface_id = UUID("00000000-0000-4000-8000-000000001470")
    chat_ref = "12345"
    question_id = UUID("00000000-0000-4000-8000-000000001471")
    notification_id = UUID("00000000-0000-4000-8000-000000001472")
    async with uow_factory() as uow:
        await uow.devices.upsert(
            Device(
                id=surface_id,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                client_device_id="telegram:test",
                name="Telegram",
                kind=DeviceKind.SURFACE,
                platform="telegram",
                push_provider=PushProvider.TELEGRAM,
                push_token=SecretStr(chat_ref),
                muted_kinds=frozenset(),
                status=DeviceStatus.ACTIVE,
                last_seen_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            ),
            owner,
        )
        await uow.surfaces.pairings.create_pairing(
            Pairing(
                id=UUID("00000000-0000-4000-8000-000000001473"),
                surface_id=surface_id,
                tenant_id=owner.tenant_id,
                principal_id=owner.principal_id,
                sender_id=chat_ref,
                granted_scopes=frozenset({"run.read"}),
                paired_at=NOW,
                last_message_at=NOW,
            )
        )
        waiting = run(status=RunStatus.WAITING_FOR_USER).model_copy(
            update={"principal_scopes": {"run.read"}, "updated_at": NOW}
        )
        await uow.runs.create(waiting)
        await uow.checkpoints.write(
            waiting.id,
            RunCheckpoint(
                run_id=waiting.id,
                version=1,
                status=RunStatus.WAITING_FOR_USER,
                working_state={
                    "outstanding_question_id": str(question_id),
                    "outstanding_question_text": "Send secretword now?",
                },
                created_at=NOW,
            ),
            full=True,
        )

    sent: list[str] = []

    async def deliver(
        chat_ref: str,
        text: str,
        *,
        last_inbound_at: datetime | None,
        now: datetime,
    ) -> SurfaceTransportResult:
        assert chat_ref == "12345"
        assert last_inbound_at == NOW
        assert now == NOW
        sent.append(text)
        return SurfaceTransportResult(outcome=SurfaceTransportOutcome.DELIVERED)

    transport = SurfaceNotificationTransport(
        uow_factory=uow_factory,
        principals=ConfiguredSchedulePrincipalDirectory(owner),
        deliveries={PushProvider.TELEGRAM: deliver},
        template_policies=None,
        clock=FixedClock(NOW),
        redactor=TrajectoryRedactor((("tenant", "secretword"),)),
    )
    payload = NotificationPayload(
        kind=NotificationKind.QUESTION_ASKED,
        title="The agent has a question",
        status=RunStatus.WAITING_FOR_USER,
        session_id=waiting.session_id,
        run_id=waiting.id,
        question_id=question_id,
        notification_id=notification_id,
    )
    outcome = await transport.deliver(
        PushTarget(
            device_id=surface_id,
            provider=PushProvider.TELEGRAM,
            token=SecretStr(chat_ref),
        ),
        PushMessage(
            notification_id=notification_id,
            dedupe_key=f"run.waiting_for_user:{waiting.id}:{question_id}",
            payload=payload,
            priority=0,
        ),
    )

    assert outcome.outcome is DeliveryOutcome.DELIVERED
    assert sent == [f"Send [redacted:tenant] now?\nID: {str(question_id).split('-', 1)[0]}"]
