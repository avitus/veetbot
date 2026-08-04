import hashlib
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

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
    assert b"".join([chunk async for chunk in store.stream(stored)]) == content
    await store.delete(stored)
    assert not (tmp_path / stored.storage_uri).exists()


async def test_trajectory_artifact_store_rejects_integrity_drift(tmp_path: Path) -> None:
    store = LocalTrajectoryArtifactStore(tmp_path)
    content = b'{"schema_version":1}'
    with pytest.raises(ValueError, match="digest and size"):
        await store.write(
            artifact(content).model_copy(update={"sha256": "0" * 64}),
            content,
        )

    stored = await store.write(artifact(content), content)
    (tmp_path / stored.storage_uri).write_bytes(content + b" ")
    with pytest.raises(ValueError, match="digest or size"):
        await store.read(stored)
    with pytest.raises(ValueError, match="digest or size"):
        _ = [chunk async for chunk in store.stream(stored)]
