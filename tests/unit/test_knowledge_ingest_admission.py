"""Knowledge ingestion admits only the caller's own readable text (knowledge-documents.md).

The Milestone 9 gates cover origin trust, the byte ceiling, and the secret scan.
These tests hold the remaining admission checks: a source that belongs to
another principal or has an unsupported media type is refused before the
artifact store is opened, and a source with no extractable text is refused
without retaining the upload as a knowledge source.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from agent_core.adapters.artifacts.filesystem import FilesystemArtifactStore
from agent_core.adapters.determinism import SequenceIdFactory
from agent_core.application.artifact_writer import ArtifactWriterFactory
from agent_core.domain.artifacts import ArtifactOrigin
from agent_core.domain.errors import ToolValidationError
from agent_core.domain.knowledge import KnowledgeIngestRequest, KnowledgeVisibility
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef
from agent_core.knowledge.service import KnowledgeService
from tests.contract.support import RUN_ID, SESSION_ID, memory_uow_factory, principal

DOCUMENT_ID = UUID(int=77)


async def _chunks(value: bytes) -> AsyncIterator[bytes]:
    yield value


async def _stack(tmp_path: Path, content: bytes) -> tuple[Any, KnowledgeService, ArtifactRef]:
    clock, factory = await memory_uow_factory()
    store = FilesystemArtifactStore(tmp_path / "artifacts")
    writer = ArtifactWriterFactory(
        factory, store, clock, SequenceIdFactory(UUID(int=value) for value in range(100, 200))
    )
    bound = writer.for_run(
        tenant_id=principal().tenant_id,
        principal_id=principal().principal_id,
        session_id=SESSION_ID,
        run_id=RUN_ID,
        origin=ArtifactOrigin.UPLOAD,
    )
    stored = await bound.create(_chunks(content), "notes.md", "text/markdown", TrustLevel.USER)
    async with factory() as uow:
        source = await uow.artifacts.get(stored.artifact_id, principal())
    service = KnowledgeService(
        factory,
        store,
        clock,
        SequenceIdFactory(UUID(int=value) for value in range(300, 400)),
        principal(),
    )
    return factory, service, source


def _request(source: ArtifactRef) -> KnowledgeIngestRequest:
    return KnowledgeIngestRequest(
        source=source,
        title="Notes",
        visibility=KnowledgeVisibility.PRINCIPAL,
        document_id=DOCUMENT_ID,
    )


@pytest.mark.parametrize(
    ("update", "message"),
    [
        ({"principal_id": "principal-b"}, "outside the caller scope"),
        ({"tenant_id": "tenant-b"}, "outside the caller scope"),
        ({"media_type": "image/png"}, "unsupported knowledge media type 'image/png'"),
    ],
)
async def test_a_foreign_or_unsupported_source_is_refused_before_it_is_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, update: dict[str, str], message: str
) -> None:
    factory, service, source = await _stack(tmp_path, b"# Plan\n\nShip on Friday.")

    async def unexpected_open(*_args: object, **_kwargs: object) -> AsyncIterator[bytes]:
        raise AssertionError("a refused source was opened")

    monkeypatch.setattr(FilesystemArtifactStore, "open_verified", unexpected_open)

    with pytest.raises(ToolValidationError, match=message):
        await service.ingest(
            _request(source.model_copy(update=update)), origin_trust=TrustLevel.USER
        )
    async with factory() as uow:
        assert await uow.knowledge.latest(source.tenant_id, DOCUMENT_ID) is None


@pytest.mark.parametrize("content", [b"", b"   \n\t\n  "])
async def test_a_source_without_text_is_refused_and_not_retained(
    tmp_path: Path, content: bytes
) -> None:
    factory, service, source = await _stack(tmp_path, content)

    with pytest.raises(ToolValidationError, match="no extractable text"):
        await service.ingest(_request(source), origin_trust=TrustLevel.USER)

    async with factory() as uow:
        assert await uow.knowledge.latest(source.tenant_id, DOCUMENT_ID) is None
        unchanged = await uow.artifacts.get(source.id, principal())
    assert unchanged.origin == "upload"
    assert unchanged.expires_at == source.expires_at
