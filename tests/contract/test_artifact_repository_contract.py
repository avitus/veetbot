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
    with pytest.raises(NotFoundError):
        await repository.get(artifact.id, Principal(tenant_id="tenant-a", principal_id="user-b"))

    assert artifact.expires_at is not None
    expired_at = artifact.expires_at + timedelta(seconds=1)
    assert await repository.list_expired(expired_at, limit=10) == [artifact]
    assert await repository.delete_expired(artifact.id, now=expired_at) is True
    assert await repository.delete_expired(artifact.id, now=expired_at) is False


def _upload(**overrides: object) -> ArtifactRef:
    now = datetime(2026, 9, 23, tzinfo=UTC)
    values: dict[str, object] = {
        "id": UUID(int=80),
        "tenant_id": "tenant-a",
        "principal_id": "user-a",
        "session_id": UUID(int=81),
        "run_id": None,
        "name": "notes.md",
        "media_type": "text/markdown",
        "storage_uri": "",
        "sha256": "2" * 64,
        "size_bytes": 20,
        "origin": "upload",
        "trust": TrustLevel.EXTERNAL_UNTRUSTED,
        "expires_at": now + timedelta(hours=24),
        "created_at": now,
        "metadata": {"attachment": {"kind": "text"}},
    }
    values.update(overrides)
    return ArtifactRef.model_validate(values)


def test_only_uploads_and_knowledge_sources_may_have_no_run() -> None:
    assert _upload().run_id is None
    assert _upload(origin="knowledge_source").run_id is None
    with pytest.raises(ValueError, match="only an upload"):
        _upload(origin="tool_output")


async def test_claiming_an_upload_binds_the_run_once_and_ends_its_expiry() -> None:
    repository = InMemoryArtifactRepository()
    owner = Principal(tenant_id="tenant-a", principal_id="user-a")
    upload = await repository.create(_upload())
    first_run, second_run = UUID(int=82), UUID(int=83)

    claimed = await repository.claim_upload(
        upload.id, owner, session_id=upload.session_id, run_id=first_run, auto_ingest=True
    )
    assert claimed.run_id == first_run
    assert claimed.expires_at is None
    assert claimed.metadata["auto_ingest"] == "pending"
    assert await repository.get(upload.id, owner) == claimed

    again = await repository.claim_upload(
        upload.id, owner, session_id=upload.session_id, run_id=second_run, auto_ingest=False
    )
    assert again == claimed


@pytest.mark.parametrize(
    ("principal", "session_id", "origin"),
    [
        (Principal(tenant_id="tenant-b", principal_id="user-a"), UUID(int=81), "upload"),
        (Principal(tenant_id="tenant-a", principal_id="user-b"), UUID(int=81), "upload"),
        (Principal(tenant_id="tenant-a", principal_id="user-a"), UUID(int=99), "upload"),
        (Principal(tenant_id="tenant-a", principal_id="user-a"), UUID(int=81), "tool_output"),
    ],
)
async def test_a_foreign_or_non_upload_artifact_cannot_be_claimed(
    principal: Principal, session_id: UUID, origin: str
) -> None:
    repository = InMemoryArtifactRepository()
    artifact = _upload(origin=origin, run_id=None if origin == "upload" else UUID(int=84))
    await repository.create(artifact)
    with pytest.raises(NotFoundError):
        await repository.claim_upload(
            artifact.id, principal, session_id=session_id, run_id=UUID(int=82), auto_ingest=True
        )


async def test_pending_ingestion_is_listed_in_upload_order_and_recorded() -> None:
    repository = InMemoryArtifactRepository()
    owner = Principal(tenant_id="tenant-a", principal_id="user-a")
    later = _upload(id=UUID(int=85), created_at=datetime(2026, 9, 23, 1, tzinfo=UTC))
    earlier = _upload()
    for artifact in (later, earlier):
        await repository.create(artifact)
        await repository.claim_upload(
            artifact.id,
            owner,
            session_id=artifact.session_id,
            run_id=UUID(int=82),
            auto_ingest=True,
        )
    pending = await repository.pending_auto_ingest(owner, limit=10)
    assert [artifact.id for artifact in pending] == [earlier.id, later.id]
    assert (
        await repository.pending_auto_ingest(
            Principal(tenant_id="tenant-a", principal_id="user-b"), limit=10
        )
        == []
    )

    refused = await repository.record_auto_ingest(
        earlier.id, owner, state="refused", reason="secret_scan", attempts=1
    )
    assert refused.metadata["auto_ingest"] == "refused"
    assert refused.metadata["auto_ingest_reason"] == "secret_scan"
    assert refused.metadata["attachment"] == {"kind": "text"}
    assert [artifact.id for artifact in await repository.pending_auto_ingest(owner, limit=10)] == [
        later.id
    ]
