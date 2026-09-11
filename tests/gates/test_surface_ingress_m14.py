"""Milestone 14 ingress transaction and pairing ceremony gates."""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.identity import ConfiguredSchedulePrincipalDirectory
from agent_core.domain.approvals import (
    ApprovalRequest,
    ApprovalResolutionType,
    ApprovalStatus,
)
from agent_core.domain.devices import Device, DeviceKind, DeviceStatus, PushProvider
from agent_core.domain.policies import (
    ActionKind,
    PolicyDecision,
    PolicyDecisionType,
    RiskLevel,
)
from agent_core.domain.runs import RunStatus
from agent_core.domain.surfaces import (
    InboundDisposition,
    Pairing,
    SurfaceChatKind,
    SurfaceInboundMessage,
    SurfaceMessageKind,
    SurfaceSession,
    issue_pairing_code,
)
from tests.contract.support import agent, memory_uow_factory, principal, run, session

NOW = datetime(2026, 9, 10, 23, 0, tzinfo=UTC)
SURFACE_ID = UUID("00000000-0000-4000-8000-000000001440")
SURFACE_SESSION_ID = UUID("00000000-0000-4000-8000-000000001441")
SURFACE_RUN_ID = UUID("00000000-0000-4000-8000-000000001442")


def _application_types() -> tuple[type[Any], type[Any]]:
    spec = importlib.util.find_spec("agent_core.application.surfaces")
    assert spec is not None, "Milestone 14 surface ingress has not been implemented"
    from agent_core.application.surfaces import PreparedSurfaceSubmission, SurfaceIngressService

    return PreparedSurfaceSubmission, SurfaceIngressService


def test_surface_notices_are_bounded_human_text() -> None:
    from agent_core.application.surfaces import surface_notice_text

    assert surface_notice_text("surface.active_run") == "Still working; /stop to cancel."
    assert "/approve ID" in surface_notice_text("surface.help")
    assert "surface." not in surface_notice_text("surface.unknown")


def _message(update_id: str, text: str, *, sender: str = "sender-1") -> SurfaceInboundMessage:
    return SurfaceInboundMessage(
        surface_id=SURFACE_ID,
        provider=PushProvider.WHATSAPP,
        external_update_id=update_id,
        sender_id=sender,
        sender_label="Owner",
        chat_ref=sender,
        chat_kind=SurfaceChatKind.DIRECT,
        message_kind=SurfaceMessageKind.TEXT,
        text=text,
        received_at=NOW,
    )


async def _noop_notice(chat_ref: str, reason_code: str) -> None:
    del chat_ref, reason_code


async def test_unpaired_sender_is_content_free_and_replay_is_a_noop() -> None:
    prepared_type, service_type = _application_types()
    del prepared_type
    _, uow_factory = await memory_uow_factory()
    submitted: list[str] = []
    notices: list[tuple[str, str]] = []

    async def submit(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        submitted.append("called")
        raise AssertionError("unpaired content reached submission")

    async def create_session(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise AssertionError("unpaired content created a session")

    async def notice(chat_ref: str, reason_code: str) -> None:
        notices.append((chat_ref, reason_code))

    service = service_type(
        uow_factory=uow_factory,
        principals=ConfiguredSchedulePrincipalDirectory(principal()),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        create_session=create_session,
        submit=submit,
        dispatch=lambda prepared: None,
        notice=notice,
    )

    first = await service.ingest(_message("wamid.unpaired", "private body"))
    replay = await service.ingest(_message("wamid.unpaired", "different body"))
    second = await service.ingest(_message("wamid.unpaired.2", "another body"))

    assert first.disposition is InboundDisposition.REJECTED_UNPAIRED
    assert replay.replayed is True
    assert second.disposition is InboundDisposition.REJECTED_UNPAIRED
    assert submitted == []
    assert notices == [("sender-1", "surface.unpaired")]
    async with uow_factory() as uow:
        receipt = await uow.surfaces.receipts.get(SURFACE_ID, "wamid.unpaired")
        assert receipt is not None
        assert "private body" not in repr(receipt)
        assert await uow.runs.latest_for_session(session().id, principal()) is None


async def test_approval_command_is_scope_guarded_idempotent_and_first_wins() -> None:
    from agent_core.runtime.executor import SurfaceRunStateWriter

    _, service_type = _application_types()
    _, uow_factory = await memory_uow_factory()
    caller = principal().model_copy(
        update={"scopes": {"approval.resolve", "run.read", "run.write"}}
    )
    approval_id = UUID("00000000-0000-4000-8000-000000001480")
    waiting = run(status=RunStatus.WAITING_FOR_APPROVAL).model_copy(
        update={"principal_scopes": set(caller.scopes), "updated_at": NOW}
    )
    async with uow_factory() as uow:
        await uow.runs.create(waiting)
        await uow.surfaces.pairings.create_pairing(
            Pairing(
                id=UUID("00000000-0000-4000-8000-000000001481"),
                surface_id=SURFACE_ID,
                tenant_id=caller.tenant_id,
                principal_id=caller.principal_id,
                sender_id="sender-1",
                granted_scopes=frozenset({"approval.resolve"}),
                paired_at=NOW,
            )
        )
        await uow.surfaces.sessions.create(
            SurfaceSession(
                id=UUID("00000000-0000-4000-8000-000000001482"),
                surface_id=SURFACE_ID,
                tenant_id=caller.tenant_id,
                principal_id=caller.principal_id,
                external_key="dm:sender-1",
                session_id=waiting.session_id,
                created_at=NOW,
            )
        )
        await uow.approvals.create(
            ApprovalRequest(
                id=approval_id,
                tenant_id=caller.tenant_id,
                principal_id=caller.principal_id,
                session_id=waiting.session_id,
                run_id=waiting.id,
                action_kind=ActionKind.TOOL_CALL,
                action_id=UUID("00000000-0000-4000-8000-000000001483"),
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

    dispatches: list[UUID] = []
    notices: list[str] = []

    async def dispatch(prepared):  # type: ignore[no-untyped-def]
        dispatches.append(prepared.run_id)

    async def notice(chat_ref: str, reason_code: str) -> None:
        del chat_ref
        notices.append(reason_code)

    async def unreachable(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise AssertionError("approval command entered run submission")

    state_writer = SurfaceRunStateWriter(FixedClock(NOW), SequenceIdFactory())
    service = service_type(
        uow_factory=uow_factory,
        principals=ConfiguredSchedulePrincipalDirectory(caller),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        create_session=unreachable,
        submit=unreachable,
        dispatch=dispatch,
        notice=notice,
        stop_run=state_writer.stop,
        resume_after_approval=state_writer.requeue_after_approval,
    )

    first = await service.ingest(_message("wamid.approve.1", f"/approve {approval_id}"))
    replay = await service.ingest(_message("wamid.approve.2", f"/approve {approval_id}"))
    denied = await service.ingest(_message("wamid.approve.3", f"/deny {approval_id}"))

    assert first.reason_code == "surface.approval_resolved"
    assert replay.reason_code == "surface.approval_resolved"
    assert denied.reason_code == "surface.approval_already_resolved"
    assert dispatches == [waiting.id]
    assert notices == [
        "surface.approval_resolved",
        "surface.approval_resolved",
        "surface.approval_already_resolved",
    ]
    async with uow_factory() as uow:
        approval = await uow.approvals.get(approval_id, caller)
        stored_run = await uow.runs.get(waiting.id, caller)
    assert approval.resolution is ApprovalResolutionType.APPROVE_ONCE
    assert stored_run.status is RunStatus.QUEUED


async def test_stop_command_uses_the_runtime_writer_and_current_scope() -> None:
    from agent_core.runtime.executor import SurfaceRunStateWriter

    _, service_type = _application_types()
    _, uow_factory = await memory_uow_factory()
    caller = principal().model_copy(update={"scopes": {"run.cancel"}})
    queued = run(status=RunStatus.QUEUED).model_copy(
        update={"principal_scopes": {"run.cancel"}, "updated_at": NOW}
    )
    async with uow_factory() as uow:
        await uow.runs.create(queued)
        await uow.surfaces.pairings.create_pairing(
            Pairing(
                id=UUID("00000000-0000-4000-8000-000000001484"),
                surface_id=SURFACE_ID,
                tenant_id=caller.tenant_id,
                principal_id=caller.principal_id,
                sender_id="sender-1",
                granted_scopes=frozenset({"run.cancel"}),
                paired_at=NOW,
            )
        )
        await uow.surfaces.sessions.create(
            SurfaceSession(
                id=UUID("00000000-0000-4000-8000-000000001485"),
                surface_id=SURFACE_ID,
                tenant_id=caller.tenant_id,
                principal_id=caller.principal_id,
                external_key="dm:sender-1",
                session_id=queued.session_id,
                created_at=NOW,
            )
        )

    async def unreachable(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise AssertionError("stop command entered run submission")

    state_writer = SurfaceRunStateWriter(FixedClock(NOW), SequenceIdFactory())
    service = service_type(
        uow_factory=uow_factory,
        principals=ConfiguredSchedulePrincipalDirectory(caller),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        create_session=unreachable,
        submit=unreachable,
        dispatch=lambda prepared: None,
        notice=lambda chat_ref, reason_code: _noop_notice(chat_ref, reason_code),
        stop_run=state_writer.stop,
        resume_after_approval=state_writer.requeue_after_approval,
    )

    stopped = await service.ingest(_message("wamid.stop.1", "/stop"))
    no_active = await service.ingest(_message("wamid.stop.2", "/stop"))

    assert stopped.reason_code == "surface.run_stopped"
    assert no_active.reason_code == "surface.no_active_run"
    async with uow_factory() as uow:
        stored = await uow.runs.get(queued.id, caller)
    assert stored.status is RunStatus.CANCELLED


async def test_pairing_then_submission_intersects_fresh_authority_and_dedupes() -> None:
    prepared_type, service_type = _application_types()
    _, uow_factory = await memory_uow_factory()
    caller = principal().model_copy(
        update={"scopes": {"run.read", "run.write", "surface.read", "surface.write"}}
    )
    issued = issue_pairing_code(
        pairing_id=UUID("00000000-0000-4000-8000-000000001443"),
        surface_id=SURFACE_ID,
        tenant_id=caller.tenant_id,
        principal_id=caller.principal_id,
        created_by_principal_id=caller.principal_id,
        granted_scopes=frozenset({"run.write", "approval.resolve"}),
        label="Owner",
        now=NOW,
        expires_after=timedelta(minutes=10),
        max_attempts=5,
        code="B7zF4nQ2",
        salt=b"0123456789abcdef",
    )
    async with uow_factory() as uow:
        await uow.devices.upsert(
            Device(
                id=SURFACE_ID,
                tenant_id=caller.tenant_id,
                principal_id=caller.principal_id,
                client_device_id="whatsapp:test",
                name="Test WhatsApp",
                kind=DeviceKind.SURFACE,
                platform="whatsapp",
                muted_kinds=frozenset(),
                capabilities=frozenset(),
                status=DeviceStatus.ACTIVE,
                last_seen_at=NOW,
                created_at=NOW,
                updated_at=NOW,
            ),
            caller,
        )
        await uow.surfaces.pairings.create_code(issued.record, caller)

    submissions: list[tuple[set[str], str, str]] = []
    dispatches: list[UUID] = []

    async def create_surface_session(uow, bound_principal, update):  # type: ignore[no-untyped-def]
        created = session().model_copy(
            update={
                "id": SURFACE_SESSION_ID,
                "principal_id": bound_principal.principal_id,
                "metadata": {"surface": update.provider.value},
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        await uow.sessions.create(created)
        return created

    async def submit(uow, bound_principal, mapped_session, text, origin, authority_version):  # type: ignore[no-untyped-def]
        del uow
        submissions.append((set(bound_principal.scopes), text, authority_version))
        assert mapped_session.id == SURFACE_SESSION_ID
        assert origin["external_update_id"] == "wamid.message"
        return prepared_type(
            run_id=SURFACE_RUN_ID,
            disposition=InboundDisposition.SUBMITTED,
            dispatch_kind="dispatch",
        )

    async def dispatch(prepared):  # type: ignore[no-untyped-def]
        dispatches.append(prepared.run_id)

    async def notice(chat_ref: str, reason_code: str) -> None:
        del chat_ref, reason_code

    service = service_type(
        uow_factory=uow_factory,
        principals=ConfiguredSchedulePrincipalDirectory(caller),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        create_session=create_surface_session,
        submit=submit,
        dispatch=dispatch,
        notice=notice,
    )

    paired = await service.ingest(_message("wamid.pair", "/pair B7zF4nQ2"))
    first = await service.ingest(_message("wamid.message", "hello"))
    replay = await service.ingest(_message("wamid.message", "changed"))

    assert paired.disposition is InboundDisposition.COMMAND_HANDLED
    assert first.disposition is InboundDisposition.SUBMITTED
    assert replay.replayed is True
    assert submissions == [({"run.write"}, "hello", submissions[0][2])]
    assert submissions[0][2].startswith("configured:")
    assert dispatches == [SURFACE_RUN_ID]
    async with uow_factory() as uow:
        pairing = await uow.surfaces.pairings.live_pairing(SURFACE_ID, "sender-1")
        mapping = await uow.surfaces.sessions.live(SURFACE_ID, "dm:sender-1")
        surface = await uow.devices.get(SURFACE_ID, caller)
        assert pairing is not None
        assert mapping is not None
        assert mapping.last_inbound_at == NOW
        assert surface.push_provider is PushProvider.WHATSAPP
        assert surface.push_token is not None
        assert surface.push_token.get_secret_value() == "sender-1"


async def test_failed_pairing_threshold_locks_before_a_correct_code_is_verified() -> None:
    _, service_type = _application_types()
    _, uow_factory = await memory_uow_factory()
    caller = principal().model_copy(update={"scopes": {"run.write"}})
    issued = issue_pairing_code(
        pairing_id=UUID("00000000-0000-4000-8000-0000000014a0"),
        surface_id=SURFACE_ID,
        tenant_id=caller.tenant_id,
        principal_id=caller.principal_id,
        created_by_principal_id=caller.principal_id,
        granted_scopes=frozenset({"run.write"}),
        label=None,
        now=NOW,
        expires_after=timedelta(minutes=10),
        max_attempts=5,
        code="B7zF4nQ2",
        salt=b"0123456789abcdef",
    )
    async with uow_factory() as uow:
        await uow.surfaces.pairings.create_code(issued.record, caller)

    notices: list[str] = []

    async def notice(chat_ref: str, reason_code: str) -> None:
        del chat_ref
        notices.append(reason_code)

    async def unreachable(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise AssertionError("locked pairing entered submission")

    service = service_type(
        uow_factory=uow_factory,
        principals=ConfiguredSchedulePrincipalDirectory(caller),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        create_session=unreachable,
        submit=unreachable,
        dispatch=lambda prepared: None,
        notice=notice,
    )

    results = [
        await service.ingest(_message(f"wamid.wrong.{attempt}", "/pair wrong"))
        for attempt in range(5)
    ]
    locked = await service.ingest(_message("wamid.correct-but-locked", "/pair B7zF4nQ2"))

    assert [result.reason_code for result in results[:4]] == ["surface.pairing_invalid"] * 4
    assert results[4].reason_code == "surface.pairing_locked"
    assert locked.reason_code == "surface.pairing_locked"
    assert notices == ["surface.pairing_invalid", "surface.pairing_locked"]
    async with uow_factory() as uow:
        lockout = await uow.surfaces.pairings.get_lockout(SURFACE_ID, "sender-1")
        events = await uow.process_events.list()
    assert lockout is not None and lockout.locked_until == NOW + timedelta(hours=1)
    assert any(event.event_type == "surface.pairing.locked" for event in events)


async def test_admission_denial_happens_before_session_or_content_write() -> None:
    _, service_type = _application_types()
    _, uow_factory = await memory_uow_factory()
    caller = principal().model_copy(update={"scopes": {"run.write"}})
    async with uow_factory() as uow:
        await uow.surfaces.pairings.create_pairing(
            Pairing(
                id=UUID("00000000-0000-4000-8000-000000001450"),
                surface_id=SURFACE_ID,
                tenant_id=caller.tenant_id,
                principal_id=caller.principal_id,
                sender_id="sender-1",
                granted_scopes=frozenset({"run.write"}),
                paired_at=NOW,
            )
        )

    class RejectAdmission:
        async def check(self, tenant_id, reservation, now):  # type: ignore[no-untyped-def]
            del tenant_id, reservation, now
            return SimpleNamespace(allowed=False, reason_code="surface.daily_cost_limit")

    async def forbidden(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        raise AssertionError("denied update crossed the admission boundary")

    notices: list[str] = []

    async def notice(chat_ref: str, reason_code: str) -> None:
        del chat_ref
        notices.append(reason_code)

    service = service_type(
        uow_factory=uow_factory,
        principals=ConfiguredSchedulePrincipalDirectory(caller),
        admission=RejectAdmission(),
        max_cost_reservation=None,
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        create_session=forbidden,
        submit=forbidden,
        dispatch=lambda prepared: None,
        notice=notice,
    )

    result = await service.ingest(_message("wamid.admission", "private body"))

    assert result.disposition is InboundDisposition.REJECTED_ADMISSION
    assert result.reason_code == "surface.daily_cost_limit"
    assert notices == ["surface.daily_cost_limit"]
    async with uow_factory() as uow:
        assert await uow.surfaces.sessions.live(SURFACE_ID, "dm:sender-1") is None
        receipt = await uow.surfaces.receipts.get(SURFACE_ID, "wamid.admission")
        assert receipt is not None
        assert "private body" not in repr(receipt)


async def test_per_sender_rate_limit_stops_content_before_submission() -> None:
    prepared_type, service_type = _application_types()
    _, uow_factory = await memory_uow_factory()
    caller = principal().model_copy(update={"scopes": {"run.write"}})
    async with uow_factory() as uow:
        await uow.agents.put(agent())
        await uow.surfaces.pairings.create_pairing(
            Pairing(
                id=UUID("00000000-0000-4000-8000-0000000014c0"),
                surface_id=SURFACE_ID,
                tenant_id=caller.tenant_id,
                principal_id=caller.principal_id,
                sender_id="sender-1",
                granted_scopes=frozenset({"run.write"}),
                paired_at=NOW,
            )
        )

    submitted: list[str] = []
    notices: list[str] = []

    async def create_surface_session(uow, bound_principal, update):  # type: ignore[no-untyped-def]
        del update
        created = session().model_copy(
            update={
                "id": UUID("00000000-0000-4000-8000-0000000014c1"),
                "principal_id": bound_principal.principal_id,
                "created_at": NOW,
                "updated_at": NOW,
            }
        )
        await uow.sessions.create(created)
        return created

    async def submit(uow, principal, mapped_session, text, origin, version):  # type: ignore[no-untyped-def]
        del uow, principal, mapped_session, origin, version
        submitted.append(text)
        return prepared_type(
            run_id=SURFACE_RUN_ID,
            disposition=InboundDisposition.SUBMITTED,
            dispatch_kind="dispatch",
        )

    async def notice(chat_ref: str, reason_code: str) -> None:
        del chat_ref
        notices.append(reason_code)

    service = service_type(
        uow_factory=uow_factory,
        principals=ConfiguredSchedulePrincipalDirectory(caller),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        create_session=create_surface_session,
        submit=submit,
        dispatch=lambda prepared: None,
        notice=notice,
        per_sender_messages_per_minute=1,
    )

    first = await service.ingest(_message("wamid.rate.1", "stored once"))
    second = await service.ingest(_message("wamid.rate.2", "must not be stored"))

    assert first.disposition is InboundDisposition.SUBMITTED
    assert second.disposition is InboundDisposition.REJECTED_RATE
    assert submitted == ["stored once"]
    assert notices == ["surface.rate_limited"]
    async with uow_factory() as uow:
        receipt = await uow.surfaces.receipts.get(SURFACE_ID, "wamid.rate.2")
    assert receipt is not None
    assert "must not be stored" not in repr(receipt)


async def test_active_run_rejects_while_waiting_run_receives_plain_input() -> None:
    prepared_type, service_type = _application_types()
    _, uow_factory = await memory_uow_factory()
    caller = principal().model_copy(update={"scopes": {"run.write"}})
    active = run(status=RunStatus.RUNNING).model_copy(
        update={"principal_scopes": {"run.write"}, "updated_at": NOW}
    )
    async with uow_factory() as uow:
        await uow.agents.put(agent())
        await uow.runs.create(active)
        await uow.surfaces.pairings.create_pairing(
            Pairing(
                id=UUID("00000000-0000-4000-8000-000000001490"),
                surface_id=SURFACE_ID,
                tenant_id=caller.tenant_id,
                principal_id=caller.principal_id,
                sender_id="sender-1",
                granted_scopes=frozenset({"run.write"}),
                paired_at=NOW,
            )
        )
        await uow.surfaces.sessions.create(
            SurfaceSession(
                id=UUID("00000000-0000-4000-8000-000000001491"),
                surface_id=SURFACE_ID,
                tenant_id=caller.tenant_id,
                principal_id=caller.principal_id,
                external_key="dm:sender-1",
                session_id=active.session_id,
                created_at=NOW,
            )
        )

    submitted: list[str] = []
    dispatched: list[UUID] = []
    notices: list[str] = []

    async def submit(uow, bound_principal, mapped_session, text, origin, version):  # type: ignore[no-untyped-def]
        del uow, bound_principal, mapped_session, origin, version
        submitted.append(text)
        return prepared_type(
            run_id=active.id,
            disposition=InboundDisposition.INPUT_DELIVERED,
            dispatch_kind="resume",
        )

    async def notice(chat_ref: str, reason_code: str) -> None:
        del chat_ref
        notices.append(reason_code)

    service = service_type(
        uow_factory=uow_factory,
        principals=ConfiguredSchedulePrincipalDirectory(caller),
        clock=FixedClock(NOW),
        ids=SequenceIdFactory(),
        create_session=lambda *args: None,
        submit=submit,
        dispatch=lambda prepared: dispatched.append(prepared.run_id),
        notice=notice,
    )

    rejected = await service.ingest(_message("wamid.active", "first"))
    assert rejected.disposition is InboundDisposition.REJECTED_ACTIVE_RUN
    assert submitted == []
    async with uow_factory() as uow:
        await uow.runs.transition(
            active.id,
            RunStatus.RUNNING,
            RunStatus.WAITING_FOR_USER,
        )

    delivered = await service.ingest(_message("wamid.waiting", "answer"))

    assert delivered.disposition is InboundDisposition.INPUT_DELIVERED
    assert submitted == ["answer"]
    assert dispatched == [active.id]
    assert notices == ["surface.active_run"]
