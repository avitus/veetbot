"""Tenant-scoped artifact metadata repository contract."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from agent_core.adapters.persistence.memory import InMemoryArtifactRepository
from agent_core.domain.agents import Principal
from agent_core.domain.errors import NotFoundError
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef


def _artifact() -> ArtifactRef:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return ArtifactRef(
        id=UUID(int=70),
        tenant_id="tenant-a",
        principal_id="user-a",
        session_id=UUID(int=71),
        run_id=UUID(int=72),
        name="result.txt",
        media_type="text/plain",
        storage_uri="",
        sha256="1" * 64,
        size_bytes=12,
        origin="sandbox_export",
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
        expires_at=now + timedelta(days=30),
        created_at=now,
    )


async def test_artifact_repository_is_idempotent_and_tenant_scoped() -> None:
    repository = InMemoryArtifactRepository()
    artifact = _artifact()
    assert await repository.exists(artifact.id) is False
    assert await repository.create(artifact) == artifact
    assert await repository.exists(artifact.id) is True
    assert await repository.create(artifact) == artifact
    assert (
        await repository.get(artifact.id, Principal(tenant_id="tenant-a", principal_id="user-a"))
        == artifact
    )
    with pytest.raises(NotFoundError):
        await repository.get(artifact.id, Principal(tenant_id="tenant-b", principal_id="user-a"))
