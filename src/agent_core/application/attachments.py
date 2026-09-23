"""Chat attachment rules at the application boundary (ADR-0118)."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.artifacts import (
    ATTACHMENT_METADATA_KEY,
    AttachmentContent,
    AttachmentRead,
    StoredArtifactRef,
)
from agent_core.domain.errors import AttachmentValidationError, NotFoundError
from agent_core.domain.messages import (
    ContentPart,
    FileReferencePart,
    ImageReferencePart,
    TextPart,
)
from agent_core.domain.trajectory import ArtifactRef
from agent_core.domain.views import ContentBlock, ImageContentBlock, TextContentBlock
from agent_core.model.attachments import (
    KNOWLEDGE_MEDIA_TYPES,
    MAX_ATTACHMENTS_PER_MESSAGE,
    PDF_MEDIA_TYPE,
)
from agent_core.ports.artifacts import ArtifactStore
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory

logger = logging.getLogger(__name__)


async def admit_content(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    session_id: UUID,
    content: Sequence[ContentBlock],
    *,
    now: datetime,
) -> list[ContentPart]:
    """Step 5 of the submit handler: rebuild parts from the caller's uploads.

    A block naming anything but a live upload of the caller in this session is
    `not_found`, exactly like a missing identifier; the stored part carries the
    upload's recorded type, name, size, and page count, never the client's.
    """

    references = [block for block in content if not isinstance(block, TextContentBlock)]
    if len(references) > MAX_ATTACHMENTS_PER_MESSAGE:
        raise AttachmentValidationError(
            f"A message may carry at most {MAX_ATTACHMENTS_PER_MESSAGE} attachments."
        )
    parts: list[ContentPart] = []
    for block in content:
        if isinstance(block, TextContentBlock):
            parts.append(TextPart(text=block.text))
            continue
        artifact = await uow.artifacts.get(block.artifact_id, principal)
        facts = artifact.metadata.get(ATTACHMENT_METADATA_KEY)
        if (
            artifact.session_id != session_id
            or artifact.origin not in {"upload", "knowledge_source"}
            or not isinstance(facts, dict)
            or (artifact.expires_at is not None and artifact.expires_at <= now)
        ):
            raise NotFoundError("artifact not found")
        is_image = facts.get("kind") == "image"
        if isinstance(block, ImageContentBlock) and not is_image:
            raise AttachmentValidationError("An image block must name an uploaded image.")
        if is_image:
            parts.append(
                ImageReferencePart(
                    artifact_id=artifact.id,
                    media_type=artifact.media_type,
                    detail=block.detail if isinstance(block, ImageContentBlock) else "auto",
                    filename=artifact.name,
                    size_bytes=artifact.size_bytes,
                )
            )
            continue
        page_count = facts.get("page_count")
        parts.append(
            FileReferencePart(
                artifact_id=artifact.id,
                media_type=artifact.media_type,
                filename=artifact.name,
                size_bytes=artifact.size_bytes,
                page_count=page_count if isinstance(page_count, int) else None,
            )
        )
    if not parts:
        raise ValueError("content must contain at least one block")
    return parts


def attachment_title(parts: Sequence[ContentPart]) -> str | None:
    """Name a chat that starts with attachments alone after its first file."""

    for part in parts:
        if isinstance(part, (ImageReferencePart, FileReferencePart)) and part.filename:
            return part.filename
    return None


async def claim_attachments(
    uow: RepositoryUnitOfWork,
    principal: Principal,
    session_id: UUID,
    run_id: UUID,
    parts: Sequence[ContentPart],
    *,
    owner_sent: bool,
) -> None:
    """Bind each attached upload to the run; mark owner-sent documents for knowledge."""

    ingest = owner_sent and "knowledge.write" in principal.scopes
    claimed: set[UUID] = set()
    for part in parts:
        if not isinstance(part, (ImageReferencePart, FileReferencePart)):
            continue
        if part.artifact_id in claimed:
            continue
        claimed.add(part.artifact_id)
        readable = part.media_type != PDF_MEDIA_TYPE or part.page_count is not None
        await uow.artifacts.claim_upload(
            part.artifact_id,
            principal,
            session_id=session_id,
            run_id=run_id,
            auto_ingest=ingest and part.media_type in KNOWLEDGE_MEDIA_TYPES and readable,
        )


class StoredAttachmentResolver:
    """Release attachment bytes from the artifact store for one run (ADR-0118)."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        store: ArtifactStore,
        principal: Principal,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._store = store
        self._principal = principal
        self._clock = clock

    async def resolve(
        self, reads: Sequence[AttachmentRead], *, run_id: UUID
    ) -> dict[UUID, AttachmentContent]:
        if not reads:
            return {}
        now = self._clock.now()
        released: dict[UUID, ArtifactRef] = {}
        async with self._uow_factory() as uow:
            try:
                run = await uow.runs.get(run_id, self._principal)
            except NotFoundError:
                return {}
            for read in reads:
                try:
                    artifact = await uow.artifacts.get(read.artifact_id, self._principal)
                except NotFoundError:
                    continue
                if (
                    artifact.session_id == run.session_id
                    and artifact.origin in {"upload", "knowledge_source"}
                    and ATTACHMENT_METADATA_KEY in artifact.metadata
                    and (artifact.expires_at is None or artifact.expires_at > now)
                ):
                    released[artifact.id] = artifact
        contents: dict[UUID, AttachmentContent] = {}
        for read in reads:
            chosen = released.get(read.artifact_id)
            if chosen is None:
                continue
            try:
                contents[chosen.id] = await self._read(chosen, read.max_bytes)
            except Exception:  # an unreadable object renders as unavailable, never fails a turn
                logger.warning("attachment_read_failed", extra={"artifact_id": str(chosen.id)})
        return contents

    async def _read(self, artifact: ArtifactRef, max_bytes: int) -> AttachmentContent:
        ref = StoredArtifactRef(
            artifact_id=artifact.id,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            media_type=artifact.media_type,
        )
        if artifact.size_bytes <= max_bytes:
            stream = await self._store.open_verified(ref, tenant_id=artifact.tenant_id)
            data = b"".join([chunk async for chunk in stream])
            return AttachmentContent(artifact_id=artifact.id, data=data)
        # A text excerpt stops early, so the whole-object hash cannot be checked.
        collected = bytearray()
        stream = self._store.open(ref, tenant_id=artifact.tenant_id)
        try:
            async for chunk in stream:
                collected.extend(chunk)
                if len(collected) >= max_bytes:
                    break
        finally:
            closer = getattr(stream, "aclose", None)
            if closer is not None:
                await closer()
        return AttachmentContent(
            artifact_id=artifact.id, data=bytes(collected[:max_bytes]), truncated=True
        )
