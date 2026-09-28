"""Only bounded, verified images in the caller's conversation reach generation."""

import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from uuid import UUID

import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.application.media_inputs import StoredMediaInputResolver
from agent_core.bootstrap import build
from agent_core.domain.artifacts import ArtifactMetadata, ArtifactOrigin
from agent_core.domain.media import MediaInputError
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef
from tests.contract.test_media_generation_provider_contract import PNG
from tests.integration.m2_support import memory_settings


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data


@pytest.mark.parametrize(
    "case",
    [
        "upload",
        "generated",
        "exported",
        "jpeg",
        "webp",
        "foreign_owner",
        "foreign_tenant",
        "foreign_session",
        "unclaimed",
        "expired",
        "knowledge",
        "tool_output",
        "missing",
        "missing_run",
        "scope",
        "mime",
        "signature",
        "checksum",
        "size",
        "aggregate",
    ],
)
async def test_media_input_release_boundary(tmp_path: Path, case: str) -> None:
    settings = replace(memory_settings(), artifact_root=tmp_path)
    async with build(settings=settings) as composition:
        principal = composition.principal
        run_id = await composition.runs.submit("hi")
        run = await composition.runs.wait_terminal(run_id)
        other = await composition.services.sessions.create(principal, "general", {})
        data = {
            "jpeg": b"\xff\xd8\xffexample",
            "webp": b"RIFF\x10\x00\x00\x00WEBPexample",
            "signature": b"not an image",
        }.get(case, PNG)
        media_type = {"jpeg": "image/jpeg", "webp": "image/webp", "mime": "image/gif"}.get(
            case, "image/png"
        )
        artifact_id = UUID(int=9001)
        store = FilesystemArtifactStore(tmp_path)
        stored = await store.put(
            chunks(data),
            ArtifactMetadata(
                artifact_id=artifact_id,
                tenant_id=principal.tenant_id,
                principal_id=principal.principal_id,
                session_id=run.session_id,
                run_id=run_id,
                origin=ArtifactOrigin.UPLOAD,
                filename="reference",
                media_type=media_type,
                size_bytes=len(data),
                sha256=hashlib.sha256(data).hexdigest(),
                trust=TrustLevel.EXTERNAL_UNTRUSTED,
                created_at=composition.clock.now(),
                expires_at=None,
            ),
        )
        values = {
            "id": artifact_id,
            "tenant_id": principal.tenant_id,
            "principal_id": principal.principal_id,
            "session_id": run.session_id,
            "run_id": run_id,
            "name": "reference",
            "media_type": media_type,
            "storage_uri": "opaque",
            "sha256": stored.sha256,
            "size_bytes": stored.size_bytes,
            "origin": "upload",
            "trust": TrustLevel.EXTERNAL_UNTRUSTED,
            "expires_at": None,
            "created_at": composition.clock.now(),
        }
        changes: dict[str, dict[str, object]] = {
            "generated": {"origin": "model_output"},
            "exported": {"origin": "sandbox_export"},
            "foreign_owner": {"principal_id": "someone-else"},
            "foreign_tenant": {"tenant_id": "other"},
            "foreign_session": {"session_id": other.id},
            "unclaimed": {"run_id": None},
            "expired": {"expires_at": composition.clock.now() - timedelta(seconds=1)},
            "knowledge": {"origin": "knowledge_source"},
            "tool_output": {"origin": "tool_output"},
            "checksum": {"sha256": "0" * 64},
            "size": {"size_bytes": 20 * 1024 * 1024 + 1},
            "aggregate": {"size_bytes": 20 * 1024 * 1024},
        }
        async with composition.uow_factory() as uow:
            await uow.artifacts.create(
                ArtifactRef.model_validate({**values, **changes.get(case, {})})
            )
        resolver = StoredMediaInputResolver(
            uow_factory=composition.uow_factory, store=store, clock=composition.clock
        )
        if case == "scope":
            principal = principal.model_copy(
                update={"scopes": principal.scopes - {"artifact.read"}}
            )
        ids = [UUID(int=9999) if case == "missing" else artifact_id] * (
            4 if case == "aggregate" else 1
        )
        if case in {"upload", "generated", "exported", "jpeg", "webp"}:
            result = await resolver.resolve(ids, run_id=run_id, principal=principal, video=False)
            assert [image.data for image in result] == [data]
            assert result[0].media_type == media_type
        else:
            with pytest.raises(MediaInputError):
                await resolver.resolve(
                    ids,
                    run_id=UUID(int=9999) if case == "missing_run" else run_id,
                    principal=principal,
                    video=False,
                )
