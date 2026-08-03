import hashlib
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from agent_core.adapters.artifacts.local import LocalTrajectoryArtifactStore
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef
from tests.contract.support import NOW, PRINCIPAL_ID, RUN_ID, SESSION_ID, TENANT


def artifact(content: bytes) -> ArtifactRef:
    return ArtifactRef(
        id=UUID(int=101),
        tenant_id=TENANT,
        principal_id=PRINCIPAL_ID,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        name="trajectory.json",
        media_type="application/json",
        storage_uri="",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        trust=TrustLevel.EXTERNAL_UNTRUSTED,
        expires_at=NOW + timedelta(days=30),
        created_at=NOW,
    )


async def test_trajectory_artifact_store_round_trip_and_delete(tmp_path: Path) -> None:
    store = LocalTrajectoryArtifactStore(tmp_path)
    content = b'{"schema_version":1}'
    stored = await store.write(artifact(content), content)
    assert stored.storage_uri.startswith("trajectory/")
    assert await store.read(stored) == content
    await store.delete(stored)
    assert not (tmp_path / stored.storage_uri).exists()
