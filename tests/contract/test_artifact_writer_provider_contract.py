"""Artifact writer providers bind platform identity before a tool writes."""

from typing import cast
from uuid import UUID

from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.ports.artifacts import ArtifactWriterProvider


class _Provider:
    def __init__(self) -> None:
        self.bound: dict[str, object] | None = None

    def for_run(self, **values: object) -> object:
        self.bound = dict(values)
        return object()


def test_artifact_writer_provider_binds_run_and_tenant() -> None:
    provider = _Provider()
    cast(ArtifactWriterProvider, provider).for_run(
        tenant_id="tenant-a",
        principal_id="user-a",
        session_id=UUID(int=80),
        run_id=UUID(int=81),
        origin=ArtifactOrigin.SANDBOX_EXPORT,
    )
    assert provider.bound is not None
    assert provider.bound == {
        "tenant_id": "tenant-a",
        "principal_id": "user-a",
        "session_id": UUID(int=80),
        "run_id": UUID(int=81),
        "origin": ArtifactOrigin.SANDBOX_EXPORT,
    }
