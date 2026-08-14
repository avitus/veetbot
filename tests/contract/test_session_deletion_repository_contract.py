"""Session deletion is scoped, idempotent, and preserves retry work."""

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
from agent_core.adapters.persistence.session_deletions import (
    InMemorySessionDeletionRepository,
)
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import RunStatus
from agent_core.domain.trajectory import ArtifactRef
from tests.contract.support import NOW, RUN_ID, SESSION_ID, memory_stack, principal, run


async def _repository() -> tuple[
    InMemorySessionDeletionRepository,
    InMemoryArtifactRepository,
    InMemorySessionRepository,
    InMemoryRunRepository,
]:
    clock, sessions, runs, events = await memory_stack()
    artifacts = InMemoryArtifactRepository()
    repository = InMemorySessionDeletionRepository(
        sessions=sessions,
        runs=runs,
        events=events,
        invocations=InMemoryToolInvocationRepository(runs),
        approvals=InMemoryApprovalRepository(clock),
        checkpoints=InMemoryCheckpointRepository(),
        idempotency=InMemoryIdempotencyRepository(clock),
        usage=InMemoryUsageRepository(runs),
        trajectory_exports=InMemoryTrajectoryExportRepository(),
        artifacts=artifacts,
        memories=InMemoryMemoryStore(clock),
        traces=InMemoryTraceStore(),
        knowledge=InMemoryKnowledgeStore(clock),
    )
    return repository, artifacts, sessions, runs


async def test_delete_is_owned_idempotent_and_keeps_sanitized_artifact_work() -> None:
    repository, artifacts, sessions, runs = await _repository()
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

    assert await repository.delete(SESSION_ID, principal(), NOW) is True
    assert await repository.delete(SESSION_ID, principal(), NOW) is False
    with pytest.raises(NotFoundError):
        await sessions.get(SESSION_ID, principal())
    with pytest.raises(NotFoundError):
        await runs.get(RUN_ID, principal())

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
    repository, _artifacts, sessions, runs = await _repository()
    await runs.create(run(status=RunStatus.WAITING_FOR_USER))

    with pytest.raises(ConflictError) as raised:
        await repository.delete(SESSION_ID, principal(), NOW)
    assert raised.value.reason == "active_run_exists"
    assert await sessions.get(SESSION_ID, principal()) is not None
