"""Ingest the documents the owner sends in chat (ADR-0120).

Sending a message marks each owner-sent text, Markdown, or PDF attachment
pending; this sweep admits it through the ordinary knowledge service. The
owner sending the file is the human admission the service requires, so it
runs with `USER` origin trust; every other ingestion step still applies.
"""

from __future__ import annotations

import logging
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core.domain.agents import Principal
from agent_core.domain.artifacts import (
    AUTO_INGEST_ATTEMPTS_KEY,
    AUTO_INGEST_FAILED,
    AUTO_INGEST_INGESTED,
    AUTO_INGEST_PENDING,
    AUTO_INGEST_REFUSED,
)
from agent_core.domain.errors import NotFoundError, ToolValidationError
from agent_core.domain.knowledge import (
    DocumentAuthority,
    KnowledgeIngestRequest,
    KnowledgeVisibility,
)
from agent_core.domain.memory import Sensitivity
from agent_core.domain.policies import TrustLevel
from agent_core.domain.trajectory import ArtifactRef
from agent_core.knowledge.service import KnowledgeService
from agent_core.ports.persistence import UnitOfWorkFactory

logger = logging.getLogger(__name__)

MAX_INGEST_ATTEMPTS = 3
_REFUSAL_CODES = (
    ("secret scan", "secret_scan"),
    ("encrypted", "encrypted"),
    ("no extractable text", "no_text"),
    ("byte ceiling", "too_large"),
    ("unsupported knowledge media type", "unsupported_type"),
    ("not valid UTF-8", "not_utf8"),
    ("could not be read", "unreadable"),
)


def upload_document_id(artifact: ArtifactRef) -> UUID:
    """One document per file content, so the same file sent twice is one document."""

    return uuid5(
        NAMESPACE_URL,
        f"veetbot:upload-knowledge:{artifact.tenant_id}:{artifact.principal_id}:{artifact.sha256}",
    )


def refusal_code(error: ToolValidationError) -> str:
    message = str(error)
    for fragment, code in _REFUSAL_CODES:
        if fragment in message:
            return code
    return "refused"


class UploadAutoIngest:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        knowledge: KnowledgeService,
        principal: Principal,
        batch_size: int = 10,
    ) -> None:
        self._uow_factory = uow_factory
        self._knowledge = knowledge
        self._principal = principal
        self._batch_size = batch_size

    async def sweep_once(self) -> int:
        """Ingest pending attachments in upload order; return how many were admitted."""

        async with self._uow_factory() as uow:
            pending = await uow.artifacts.pending_auto_ingest(
                self._principal, limit=self._batch_size
            )
        admitted = 0
        for artifact in pending:
            attempts = int(artifact.metadata.get(AUTO_INGEST_ATTEMPTS_KEY, 0)) + 1
            request = KnowledgeIngestRequest(
                source=artifact,
                title=artifact.name,
                visibility=KnowledgeVisibility.PRINCIPAL,
                document_id=upload_document_id(artifact),
                authority=DocumentAuthority.PRINCIPAL_SUPPLIED,
                sensitivity=Sensitivity.INTERNAL,
            )
            try:
                await self._knowledge.ingest(request, origin_trust=TrustLevel.USER)
            except ToolValidationError as error:
                await self._record(artifact, AUTO_INGEST_REFUSED, refusal_code(error), attempts)
                continue
            except NotFoundError:
                await self._record(artifact, AUTO_INGEST_REFUSED, "not_found", attempts)
                continue
            except Exception:
                logger.warning(
                    "upload_knowledge_ingest_failed",
                    extra={"artifact_id": str(artifact.id), "attempt": attempts},
                )
                state = (
                    AUTO_INGEST_FAILED if attempts >= MAX_INGEST_ATTEMPTS else AUTO_INGEST_PENDING
                )
                await self._record(artifact, state, "transient", attempts)
                continue
            await self._record(artifact, AUTO_INGEST_INGESTED, None, 0)
            admitted += 1
        return admitted

    async def _record(
        self, artifact: ArtifactRef, state: str, reason: str | None, attempts: int
    ) -> None:
        async with self._uow_factory() as uow:
            await uow.artifacts.record_auto_ingest(
                artifact.id, self._principal, state=state, reason=reason, attempts=attempts
            )
