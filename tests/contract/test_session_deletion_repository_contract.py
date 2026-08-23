"""Session deletion is scoped, idempotent, and preserves retry work."""

import inspect
from datetime import timedelta
from uuid import UUID

import pytest

from agent_core.adapters.memory.in_memory import (
    InMemoryKnowledgeStore,
    InMemoryMemoryStore,
    InMemoryTraceStore,
)
from agent_core.adapters.persistence.memory import (
    InMemoryApprovalRepository,
    InMemoryArtifactRepository,
    InMemoryCheckpointRepository,
    InMemoryIdempotencyRepository,
    InMemoryRunRepository,
    InMemorySessionRepository,
    InMemoryToolInvocationRepository,
    InMemoryTrajectoryExportRepository,
    InMemoryUsageRepository,
)
from agent_core.adapters.persistence.notifications import (
    InMemoryDeviceRegistry,
    InMemoryNotificationOutbox,
)
from agent_core.adapters.persistence.schedules import InMemoryScheduleRepository
from agent_core.adapters.persistence.session_deletions import (
    InMemorySessionDeletionRepository,
)
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.policies import RiskLevel, SideEffectClass, TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.domain.schedules import OccurrenceDisposition, ScheduleOccurrence
from agent_core.domain.tools import ToolInvocation, ToolInvocationStatus
from agent_core.domain.trajectory import ArtifactRef, TrajectoryExport
from tests.contract.support import NOW, RUN_ID, SESSION_ID, memory_stack, principal, run
from tests.contract.test_schedule_repository_contract import revision, schedule


def test_in_memory_session_deletion_requires_the_notification_outbox() -> None:
    parameter = inspect.signature(InMemorySessionDeletionRepository).parameters[
        "notification_outbox"
    ]

    assert parameter.default is inspect.Parameter.empty


async def _repository() -> tuple[
    InMemorySessionDeletionRepository,
    InMemoryArtifactRepository,
    InMemorySessionRepository,
    InMemoryRunRepository,
    InMemoryToolInvocationRepository,
    InMemoryTrajectoryExportRepository,
    InMemoryScheduleRepository,
]:
    clock, sessions, runs, events = await memory_stack()
    artifacts = InMemoryArtifactRepository()
    invocations = InMemoryToolInvocationRepository(runs)
    trajectory_exports = InMemoryTrajectoryExportRepository()
    schedules = InMemoryScheduleRepository()
    notification_outbox = InMemoryNotificationOutbox(clock, InMemoryDeviceRegistry())
    repository = InMemorySessionDeletionRepository(
        sessions=sessions,
        runs=runs,
        events=events,
        invocations=invocations,
        approvals=InMemoryApprovalRepository(clock),
        checkpoints=InMemoryCheckpointRepository(),
        idempotency=InMemoryIdempotencyRepository(clock),
        usage=InMemoryUsageRepository(runs),
        trajectory_exports=trajectory_exports,
        artifacts=artifacts,
        memories=InMemoryMemoryStore(clock),
        traces=InMemoryTraceStore(),
        knowledge=InMemoryKnowledgeStore(clock),
        schedules=schedules,
        notification_outbox=notification_outbox,
    )
    return repository, artifacts, sessions, runs, invocations, trajectory_exports, schedules


async def test_delete_is_owned_idempotent_and_keeps_sanitized_artifact_work() -> None:
    (
        repository,
        artifacts,
        sessions,
        runs,
        invocations,
        trajectory_exports,
        _schedules,
    ) = await _repository()
    await runs.create(run(status=RunStatus.COMPLETED))
    artifact = ArtifactRef(
        id=UUID(int=80),
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        name="user-visible-secret-name.txt",
        media_type="text/plain",
        storage_uri="objects/delete-me",
        sha256="8" * 64,
        size_bytes=8,
        origin="sandbox_export",
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
        expires_at=NOW + timedelta(days=1),
        created_at=NOW,
        metadata={"user_note": "do not retain"},
    )
    await artifacts.create(artifact)
    invocation = ToolInvocation(
        id=UUID(int=81),
        run_id=RUN_ID,
        session_id=SESSION_ID,
        step_number=1,
        call_id="delete-contract-call",
        tool_name="math.calculate",
        tool_version="1.0.0",
        side_effect=SideEffectClass.NONE,
        risk=RiskLevel.LOW,
        status=ToolInvocationStatus.SUCCEEDED,
        raw_arguments='{"expression":"1+1"}',
        idempotency_key="delete-contract-key",
        created_at=NOW,
        updated_at=NOW,
    )
    await invocations.create(invocation)
    await trajectory_exports.create(
        TrajectoryExport(
            export_id=UUID(int=82),
            tenant_id=artifact.tenant_id,
            principal_id=artifact.principal_id,
            run_id=RUN_ID,
            artifact=artifact,
            builder_version="delete-contract",
            ruleset_version="delete-contract",
            created_at=NOW,
        )
    )

    assert await repository.delete(SESSION_ID, principal(), NOW) is True
    assert await repository.delete(SESSION_ID, principal(), NOW) is False
    with pytest.raises(NotFoundError):
        await sessions.get(SESSION_ID, principal())
    with pytest.raises(NotFoundError):
        await runs.get(RUN_ID, principal())
    assert await invocations.find_by_idempotency_key(RUN_ID, "delete-contract-key") is None
    assert await trajectory_exports.get_for_run(RUN_ID) is None

    pending = await repository.pending_artifacts(SESSION_ID, principal(), limit=10)
    assert len(pending) == 1
    assert pending[0].id == artifact.id
    assert pending[0].name == "deleted-artifact"
    assert pending[0].metadata == {}
    assert await repository.pending_sessions(principal(), limit=10) == [SESSION_ID]

    another = Principal(tenant_id=principal().tenant_id, principal_id="another-principal")
    with pytest.raises(NotFoundError):
        await repository.pending_artifacts(SESSION_ID, another, limit=10)
    with pytest.raises(NotFoundError):
        await repository.delete(SESSION_ID, another, NOW)

    await repository.acknowledge_artifact(SESSION_ID, artifact.id, principal())
    assert await repository.pending_artifacts(SESSION_ID, principal(), limit=10) == []
    assert await repository.pending_sessions(principal(), limit=10) == []


async def test_delete_rejects_a_nonterminal_run_without_removing_the_session() -> None:
    repository, _artifacts, sessions, runs, _invocations, _exports, _schedules = await _repository()
    await runs.create(run(status=RunStatus.WAITING_FOR_USER))

    with pytest.raises(ConflictError) as raised:
        await repository.delete(SESSION_ID, principal(), NOW)
    assert raised.value.reason == "active_run_exists"
    assert await sessions.get(SESSION_ID, principal()) is not None


async def test_in_memory_deletion_erases_materialized_occurrence_links() -> None:
    repository, _artifacts, _sessions, runs, _invocations, _exports, schedules = await _repository()
    await runs.create(run(status=RunStatus.COMPLETED))
    scheduled = schedule()
    await schedules.create(scheduled, revision())
    occurrence = ScheduleOccurrence(
        id=UUID(int=83),
        schedule_id=scheduled.id,
        schedule_revision=1,
        nominal_fire_at=NOW,
        disposition=OccurrenceDisposition.MATERIALIZED,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        authority_version="authority-v1",
        materialized_at=NOW,
        created_at=NOW,
    )
    await schedules.insert(occurrence)

    erased_at = NOW + timedelta(seconds=1)
    await repository.delete(SESSION_ID, principal(), erased_at)

    [erased] = await schedules.list_occurrences(scheduled.id, principal(), limit=10)
    assert erased.session_id is None
    assert erased.run_id is None
    assert erased.links_erased_at == erased_at
