"""PostgreSQL contracts and tenant isolation for inbound surfaces."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
import yaml
from pydantic import SecretStr
from sqlalchemy import text

import agent_core.adapters.persistence.surfaces as surface_adapters
from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.adapters.identity import ConfiguredSchedulePrincipalDirectory
from agent_core.adapters.persistence.unit_of_work import PostgresUnitOfWork
from agent_core.adapters.surface_admission import (
    AllowSurfaceAdmissionController,
    PostgresSurfaceAdmissionController,
)
from agent_core.application.surfaces import (
    PreparedSurfaceSubmission,
    SurfaceIngressService,
)
from agent_core.bootstrap import build
from agent_core.config import PACKAGE_ROOT
from agent_core.domain.devices import Device, DeviceKind, DeviceStatus, PushProvider
from agent_core.domain.errors import NotFoundError
from agent_core.domain.events import NewEvent
from agent_core.domain.runs import RunLimits, RunStatus, RunUsage
from agent_core.domain.sessions import Session, SessionStatus
from agent_core.domain.surfaces import (
    InboundDisposition,
    Pairing,
    SurfaceChatKind,
    SurfaceInboundMessage,
    SurfaceLimits,
    SurfaceMessageKind,
)
from tests.contract.support import NOW, agent, run, session
from tests.contract.test_surface_repository_contract import (
    RUN_ID,
    SESSION_ID,
    SURFACE_ID,
    assert_surface_repositories_contract,
    owner,
)
from tests.integration.m2_support import database_settings


class _RollbackContractError(Exception):
    pass


class _InjectedIngressCrashError(Exception):
    pass


_SURFACE_LIMITS = SurfaceLimits.model_validate(
    yaml.safe_load((PACKAGE_ROOT / "runtime/limits.yaml").read_text(encoding="utf-8"))["surfaces"]
)


def _surface_device() -> Device:
    return Device(
        id=SURFACE_ID,
        tenant_id=owner().tenant_id,
        principal_id=owner().principal_id,
        client_device_id="telegram-owner-surface",
        name="Owner surface",
        kind=DeviceKind.SURFACE,
        platform="telegram",
        push_provider=PushProvider.TELEGRAM,
        push_token=SecretStr("contract-surface-routing-token"),
        muted_kinds=frozenset(),
        status=DeviceStatus.ACTIVE,
        last_seen_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


async def test_postgres_surface_repositories_satisfy_shared_contract() -> None:
    assert hasattr(surface_adapters, "PostgresSurfaceRepositories"), (
        "PostgreSQL surface repositories are not implemented"
    )
    async with build(settings=database_settings(), storage="postgres") as composition:
        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                assert isinstance(uow, PostgresUnitOfWork)
                await uow.agents.put(agent())
                await uow.sessions.create(
                    session().model_copy(
                        update={
                            "id": SESSION_ID,
                            "principal_id": owner().principal_id,
                        }
                    )
                )
                await uow.runs.create(
                    run().model_copy(update={"id": RUN_ID, "session_id": SESSION_ID})
                )
                await uow.devices.upsert(_surface_device(), owner())
                await assert_surface_repositories_contract(uow.surfaces)
                raise _RollbackContractError


async def test_postgres_surface_rows_are_tenant_isolated_and_rls_forced() -> None:
    tables = [
        "surface_pairing_codes",
        "surface_pairings",
        "surface_sender_lockouts",
        "surface_sessions",
        "surface_inbound_receipts",
        "surface_replies",
    ]
    async with build(settings=database_settings(), storage="postgres") as composition:
        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                assert isinstance(uow, PostgresUnitOfWork)
                await uow.session.execute(
                    text("SELECT set_config('agent_core.tenant_id', 'tenant-a', true)")
                )
                await uow.agents.put(agent())
                await uow.sessions.create(
                    session().model_copy(
                        update={"id": SESSION_ID, "principal_id": owner().principal_id}
                    )
                )
                await uow.runs.create(
                    run().model_copy(update={"id": RUN_ID, "session_id": SESSION_ID})
                )
                await uow.devices.upsert(_surface_device(), owner())
                await assert_surface_repositories_contract(uow.surfaces)
                await uow.surfaces.pairings.record_failed_pairing(
                    SURFACE_ID,
                    "locked-sender",
                    at=NOW,
                    max_attempts=1,
                    lockout_seconds=3600,
                )

                is_superuser = bool(
                    await uow.session.scalar(
                        text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
                    )
                )
                if is_superuser:
                    await uow.session.execute(
                        text("CREATE ROLE veetbot_surface_rls_probe NOLOGIN NOSUPERUSER")
                    )
                    await uow.session.execute(
                        text(
                            "GRANT SELECT ON devices, surface_pairing_codes, surface_pairings, "
                            "surface_sender_lockouts, surface_sessions, "
                            "surface_inbound_receipts, surface_replies "
                            "TO veetbot_surface_rls_probe"
                        )
                    )
                    await uow.session.execute(text("SET LOCAL ROLE veetbot_surface_rls_probe"))
                await uow.session.execute(
                    text("SELECT set_config('agent_core.tenant_id', 'tenant-b', true)")
                )
                counts = [
                    await uow.session.scalar(text(f"SELECT count(*) FROM {table}"))  # noqa: S608
                    for table in tables
                ]
                assert counts == [0, 0, 0, 0, 0, 0]

                rows = (
                    await uow.session.execute(
                        text(
                            "SELECT relname, relrowsecurity, relforcerowsecurity "
                            "FROM pg_class WHERE relname = ANY(:tables)"
                        ),
                        {"tables": tables},
                    )
                ).all()
                assert {row.relname for row in rows} == set(tables)
                assert all(row.relrowsecurity and row.relforcerowsecurity for row in rows)
                if is_superuser:
                    await uow.session.execute(text("RESET ROLE"))
                    await uow.session.execute(
                        text(
                            "REVOKE SELECT ON devices, surface_pairing_codes, surface_pairings, "
                            "surface_sender_lockouts, surface_sessions, "
                            "surface_inbound_receipts, surface_replies "
                            "FROM veetbot_surface_rls_probe"
                        )
                    )
                    await uow.session.execute(text("DROP ROLE veetbot_surface_rls_probe"))
                raise _RollbackContractError


@pytest.mark.parametrize(
    ("status", "run_cost", "now", "limit_changes", "reason_code"),
    [
        (
            RunStatus.QUEUED,
            Decimal("0"),
            NOW,
            {"max_active_runs_per_tenant": 1, "daily_cost": 100, "monthly_cost": 100},
            "surface.concurrency_limit",
        ),
        (
            RunStatus.COMPLETED,
            Decimal("5"),
            NOW,
            {"max_active_runs_per_tenant": 10, "daily_cost": 5, "monthly_cost": 100},
            "surface.daily_cost_limit",
        ),
        (
            RunStatus.COMPLETED,
            Decimal("10"),
            NOW + timedelta(days=1),
            {"max_active_runs_per_tenant": 10, "daily_cost": 5, "monthly_cost": 10},
            "surface.monthly_cost_limit",
        ),
    ],
)
async def test_postgres_surface_admission_enforces_active_daily_and_monthly_limits(
    status: RunStatus,
    run_cost: Decimal,
    now: datetime,
    limit_changes: dict[str, object],
    reason_code: str,
) -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        with pytest.raises(_RollbackContractError):
            async with composition.uow_factory() as uow:
                assert isinstance(uow, PostgresUnitOfWork)
                await uow.agents.put(agent())
                await uow.sessions.create(session())
                surface_run = run(status=status).model_copy(
                    update={
                        "limits": RunLimits(max_cost=Decimal("2")),
                        "usage": RunUsage(cost=run_cost),
                        "updated_at": NOW,
                    }
                )
                await uow.runs.create(surface_run)
                await uow.events.append(
                    NewEvent(
                        session_id=surface_run.session_id,
                        run_id=surface_run.id,
                        event_type="run.queued",
                        actor_type="surface",
                        actor_id="principal-a",
                        payload={
                            "origin": {
                                "kind": "telegram",
                                "surface_id": str(SURFACE_ID),
                                "external_update_id": "41",
                            }
                        },
                    )
                )
                limits = _SURFACE_LIMITS.model_copy(update=limit_changes)
                decision = await PostgresSurfaceAdmissionController(
                    uow.session,
                    limits,
                ).check("tenant-a", Decimal("1"), now)
                assert decision.allowed is False
                assert decision.reason_code == reason_code
                raise _RollbackContractError


async def test_postgres_ingress_crash_rolls_back_and_redelivers_once() -> None:
    surface_id = UUID("00000000-0000-4000-8000-0000000014b0")
    session_id = UUID("00000000-0000-4000-8000-0000000014b1")
    run_id = UUID("00000000-0000-4000-8000-0000000014b2")
    pairing_id = UUID("00000000-0000-4000-8000-0000000014b3")
    update_id = "43"
    bound_owner = owner().model_copy(update={"scopes": {"run.write"}})
    async with build(settings=database_settings(), storage="postgres") as composition:
        async with composition.uow_factory() as uow:
            await uow.agents.put(agent())
            await uow.devices.upsert(
                _surface_device().model_copy(update={"id": surface_id}),
                bound_owner,
            )
            await uow.surfaces.pairings.create_pairing(
                Pairing(
                    id=pairing_id,
                    surface_id=surface_id,
                    tenant_id=bound_owner.tenant_id,
                    principal_id=bound_owner.principal_id,
                    sender_id="sender-atomic",
                    granted_scopes=frozenset({"run.write"}),
                    paired_at=NOW,
                )
            )

        crash = True
        dispatches: list[UUID] = []

        async def create_surface_session(uow, principal, update):  # type: ignore[no-untyped-def]
            del update
            created = Session(
                id=session_id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                agent_id=agent().id,
                agent_version=agent().version,
                status=SessionStatus.ACTIVE,
                metadata={"surface": "telegram"},
                created_at=NOW,
                updated_at=NOW,
            )
            await uow.sessions.create(created)
            return created

        async def submit(uow, principal, mapped_session, text_value, origin, version):  # type: ignore[no-untyped-def]
            nonlocal crash
            del text_value, origin, version
            created = run().model_copy(
                update={
                    "id": run_id,
                    "session_id": mapped_session.id,
                    "tenant_id": principal.tenant_id,
                    "principal_scopes": set(principal.scopes),
                    "created_at": NOW,
                    "updated_at": NOW,
                }
            )
            await uow.runs.create(created)
            if crash:
                raise _InjectedIngressCrashError
            return PreparedSurfaceSubmission(
                run_id=created.id,
                disposition=InboundDisposition.SUBMITTED,
                dispatch_kind="dispatch",
            )

        async def notice(chat_ref: str, reason_code: str) -> None:
            del chat_ref, reason_code

        ingress = SurfaceIngressService(
            uow_factory=composition.uow_factory,
            principals=ConfiguredSchedulePrincipalDirectory(bound_owner),
            admission=AllowSurfaceAdmissionController(),
            max_cost_reservation=Decimal("1"),
            clock=FixedClock(NOW),
            ids=SequenceIdFactory(),
            create_session=create_surface_session,
            submit=submit,
            dispatch=lambda prepared: dispatches.append(prepared.run_id),
            notice=notice,
        )
        update = SurfaceInboundMessage(
            surface_id=surface_id,
            provider=PushProvider.TELEGRAM,
            external_update_id=update_id,
            sender_id="sender-atomic",
            chat_ref="sender-atomic",
            chat_kind=SurfaceChatKind.DIRECT,
            message_kind=SurfaceMessageKind.TEXT,
            text="atomic content",
            received_at=NOW,
        )

        with pytest.raises(_InjectedIngressCrashError):
            await ingress.ingest(update)
        async with composition.uow_factory() as uow:
            assert await uow.surfaces.receipts.get(surface_id, update_id) is None
            assert await uow.surfaces.sessions.live(surface_id, "dm:sender-atomic") is None
            with pytest.raises(NotFoundError):
                await uow.sessions.get(session_id, bound_owner)
            with pytest.raises(NotFoundError):
                await uow.runs.get(run_id, bound_owner)

        crash = False
        result = await ingress.ingest(update)
        replay = await ingress.ingest(update)

        assert result.disposition is InboundDisposition.SUBMITTED
        assert replay.replayed is True
        assert dispatches == [run_id]
        async with composition.uow_factory() as uow:
            assert await uow.surfaces.receipts.get(surface_id, update_id) is not None
            assert await uow.surfaces.sessions.live(surface_id, "dm:sender-atomic") is not None
            assert (await uow.runs.get(run_id, bound_owner)).id == run_id
