"""Release bounded image references only from the caller's conversation."""

import hashlib
from collections.abc import Sequence
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.artifacts import StoredArtifactRef
from agent_core.domain.errors import ArtifactIntegrityError, NotFoundError
from agent_core.domain.media import (
    MediaImage,
    MediaInputError,
    reference_limits,
    validate_media_images,
)
from agent_core.domain.trajectory import ArtifactRef
from agent_core.ports.artifacts import ArtifactStore
from agent_core.ports.determinism import Clock
from agent_core.ports.persistence import UnitOfWorkFactory


class StoredMediaInputResolver:
    def __init__(
        self, *, uow_factory: UnitOfWorkFactory, store: ArtifactStore, clock: Clock
    ) -> None:
        self._uow_factory = uow_factory
        self._store = store
        self._clock = clock

    async def resolve(
        self, artifact_ids: Sequence[UUID], *, run_id: UUID, principal: Principal, video: bool
    ) -> tuple[MediaImage, ...]:
        if "artifact.read" not in principal.scopes:
            raise MediaInputError("unavailable")
        count, per_image, total = reference_limits(video=video)
        if len(artifact_ids) > count:
            raise MediaInputError("too_large")
        selected: list[ArtifactRef] = []
        try:
            async with self._uow_factory() as uow:
                run = await uow.runs.get(run_id, principal)
                for artifact_id in artifact_ids:
                    artifact = await uow.artifacts.get(artifact_id, principal)
                    if (
                        artifact.tenant_id != principal.tenant_id
                        or artifact.principal_id != principal.principal_id
                        or artifact.session_id != run.session_id
                        or artifact.run_id is None
                        or artifact.origin not in {"upload", "model_output", "sandbox_export"}
                        or (
                            artifact.expires_at is not None
                            and artifact.expires_at <= self._clock.now()
                        )
                    ):
                        raise MediaInputError("unavailable")
                    if (
                        artifact.media_type not in {"image/png", "image/jpeg", "image/webp"}
                        or artifact.size_bytes <= 0
                    ):
                        raise MediaInputError("invalid")
                    if artifact.size_bytes > per_image:
                        raise MediaInputError("too_large")
                    selected.append(artifact)
            if sum(artifact.size_bytes for artifact in selected) > total:
                raise MediaInputError("too_large")
            images = tuple([await self._read(artifact, per_image) for artifact in selected])
        except (NotFoundError, OSError, ArtifactIntegrityError):
            raise MediaInputError("unavailable") from None
        validate_media_images(images, video=video)
        return images

    async def _read(self, artifact: ArtifactRef, maximum: int) -> MediaImage:
        ref = StoredArtifactRef(
            artifact_id=artifact.id,
            sha256=artifact.sha256,
            size_bytes=artifact.size_bytes,
            media_type=artifact.media_type,
        )
        stream = await self._store.open_verified(ref, tenant_id=artifact.tenant_id)
        collected = bytearray()
        try:
            async for chunk in stream:
                if len(collected) + len(chunk) > min(maximum, artifact.size_bytes):
                    raise MediaInputError("too_large")
                collected.extend(chunk)
        finally:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
        # Check the bytes actually released, even if a store changes after its
        # verification pass. Nothing reaches the external provider before this.
        if (
            len(collected) != artifact.size_bytes
            or hashlib.sha256(collected).hexdigest() != artifact.sha256
        ):
            raise MediaInputError("unavailable")
        return MediaImage(artifact.media_type, bytes(collected))
