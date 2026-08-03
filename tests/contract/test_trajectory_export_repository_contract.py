import hashlib
from datetime import timedelta
from uuid import UUID

from agent_core.adapters.persistence.memory import InMemoryTrajectoryExportRepository
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef, TrajectoryExport
from tests.contract.support import NOW, PRINCIPAL_ID, RUN_ID, SESSION_ID, TENANT


def exported() -> TrajectoryExport:
    content = b"trajectory"
    artifact = ArtifactRef(
        id=UUID(int=102),
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        name="trajectory.json",
        media_type="application/json",
        storage_uri="trajectory/key.json",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
        expires_at=NOW + timedelta(days=30),
        created_at=NOW,
    )
    return TrajectoryExport(
        export_id=UUID(int=103),
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        run_id=RUN_ID,
        artifact=artifact,
        builder_version="trajectory@3",
        ruleset_version="secrets@1",
        created_at=NOW,
    )


async def test_trajectory_export_is_idempotent_and_swept_after_expiry() -> None:
    repository = InMemoryTrajectoryExportRepository()
    row = exported()
    assert await repository.create(row) == row
    assert await repository.create(row) == row
    assert await repository.expire_for_principal(TENANT, PRINCIPAL_ID, NOW) == 1
    assert await repository.expire_for_principal(TENANT, PRINCIPAL_ID, NOW + timedelta(days=1)) == 0
    expired = await repository.list_expired(NOW, limit=10)
    assert expired[0].id == row.artifact.id
    retained = await repository.get_for_run(RUN_ID)
    assert retained is not None
    assert retained.artifact.expires_at == NOW
    assert await repository.delete_expired(row.artifact.id, now=NOW)
    assert await repository.get_for_run(RUN_ID) is None
