"""Deterministic in-memory skill repository."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

from agent_core.domain.errors import ConflictError, NotFoundError, SkillRevisionConflict
from agent_core.domain.policies import TrustLevel
from agent_core.domain.skills import (
    AuthoringContext,
    SkillPackage,
    SkillRef,
    SkillRevision,
    SkillSource,
    SkillStatus,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.skills import SkillPackageStore
from agent_core.skills.package import SkillPackageValidator


@dataclass(frozen=True, slots=True)
class _SkillIdentity:
    id: UUID
    source: SkillSource


class InMemorySkillRepository:
    def __init__(
        self,
        store: SkillPackageStore,
        validator: SkillPackageValidator,
        clock: Clock,
        ids: IdFactory,
    ) -> None:
        self._store = store
        self._validator = validator
        self._clock = clock
        self._ids = ids
        self._identities: dict[tuple[str, str], _SkillIdentity] = {}
        self._revisions: dict[tuple[str, str], list[SkillRevision]] = {}
        self._lock = asyncio.Lock()

    async def install(
        self,
        tenant_id: str,
        package: SkillPackage,
        source: SkillSource,
        expected_revision: int | None,
        authored_by: AuthoringContext | None,
    ) -> SkillRevision:
        if source is SkillSource.MCP:
            raise ConflictError("MCP prompt skills are read-only and session scoped")
        if (source is SkillSource.AGENT) != (authored_by is not None):
            raise ValueError("authoring provenance must be present only for agent skills")
        validated = self._validator.validate(package)
        key = (tenant_id, validated.manifest.name)
        async with self._lock:
            if authored_by is not None:
                prior = next(
                    (
                        item
                        for items in self._revisions.values()
                        for item in items
                        if item.tenant_id == tenant_id
                        and item.authored_by_invocation_id == authored_by.invocation_id
                    ),
                    None,
                )
                if prior is not None:
                    if prior.authoring_idempotency_key != authored_by.idempotency_key:
                        raise ConflictError(
                            "skill authoring invocation was reused with different arguments",
                            reason="skill_authoring_idempotency_conflict",
                        )
                    return prior.model_copy(deep=True)
            identity = self._identities.get(key)
            revisions = self._revisions.get(key, [])
            current_revision = 0 if not revisions else revisions[-1].revision
            if expected_revision is not None and expected_revision != current_revision:
                raise SkillRevisionConflict(current_revision)
            if identity is None:
                identity = _SkillIdentity(id=self._ids.new_id(), source=source)
            elif identity.source is not source:
                raise ConflictError("skill source cannot change across revisions")
            revision_number = current_revision + 1
            stored = await self._store.put(
                tenant_id,
                identity.id,
                revision_number,
                validated.archive,
            )
            revision = SkillRevision(
                skill_id=identity.id,
                tenant_id=tenant_id,
                revision=revision_number,
                manifest=validated.manifest,
                body=validated.body,
                body_tokens=validated.body_tokens,
                content_sha256=validated.content_sha256,
                package_key=stored.key,
                package_bytes=validated.package_bytes,
                file_count=validated.file_count,
                source=source,
                trust=(
                    TrustLevel.TRUSTED_CONFIGURATION
                    if source in {SkillSource.BUILTIN, SkillSource.OPERATOR}
                    else TrustLevel.EXTERNAL_UNTRUSTED
                ),
                status=SkillStatus.ACTIVE,
                authored_by_run_id=None if authored_by is None else authored_by.run_id,
                authored_by_principal_id=(
                    None if authored_by is None else authored_by.principal_id
                ),
                authored_by_invocation_id=(
                    None if authored_by is None else authored_by.invocation_id
                ),
                authoring_idempotency_key=(
                    None if authored_by is None else authored_by.idempotency_key
                ),
                created_at=self._clock.now(),
            )
            self._identities[key] = identity
            self._revisions.setdefault(key, []).append(revision)
            return revision.model_copy(deep=True)

    async def resolve(self, tenant_id: str, ref: SkillRef) -> SkillRevision:
        revisions = self._revisions.get((tenant_id, ref.name), [])
        candidates = [
            item
            for item in revisions
            if (ref.revision is None and item.status is SkillStatus.ACTIVE)
            or (ref.revision is not None and item.revision == ref.revision)
        ]
        if not candidates:
            raise NotFoundError(f"skill reference {ref} was not found")
        return candidates[-1].model_copy(deep=True)

    async def list_active(self, tenant_id: str, limit: int) -> list[SkillRevision]:
        if limit <= 0:
            return []
        latest: list[SkillRevision] = []
        for (candidate_tenant, _name), revisions in sorted(self._revisions.items()):
            if candidate_tenant != tenant_id:
                continue
            active = [item for item in revisions if item.status is SkillStatus.ACTIVE]
            if active:
                latest.append(active[-1].model_copy(deep=True))
        return latest[:limit]

    async def archive(
        self,
        tenant_id: str,
        name: str,
        revision: int,
        authored_by: AuthoringContext | None = None,
    ) -> SkillRevision:
        key = (tenant_id, name)
        async with self._lock:
            if authored_by is not None:
                prior = next(
                    (
                        item
                        for (candidate_tenant, candidate_name), items in self._revisions.items()
                        for item in items
                        if item.archived_by_invocation_id == authored_by.invocation_id
                        and candidate_tenant == tenant_id
                    ),
                    None,
                )
                if prior is not None:
                    if (
                        prior.manifest.name != name
                        or prior.revision != revision
                        or prior.archive_idempotency_key != authored_by.idempotency_key
                    ):
                        raise ConflictError(
                            "skill archive invocation was reused with different arguments",
                            reason="skill_authoring_idempotency_conflict",
                        )
                    return prior.model_copy(deep=True)
            revisions = self._revisions.get(key, [])
            active = [item for item in revisions if item.status is SkillStatus.ACTIVE]
            if not active:
                raise NotFoundError("skill revision not found")
            if active[-1].revision != revision:
                raise SkillRevisionConflict(active[-1].revision)
            for index, candidate in enumerate(revisions):
                if candidate.revision == revision:
                    revisions[index] = candidate.model_copy(
                        update={
                            "status": SkillStatus.ARCHIVED,
                            "archived_by_invocation_id": (
                                None if authored_by is None else authored_by.invocation_id
                            ),
                            "archive_idempotency_key": (
                                None if authored_by is None else authored_by.idempotency_key
                            ),
                        },
                        deep=True,
                    )
                    return revisions[index].model_copy(deep=True)
        raise NotFoundError("skill revision not found")

    def revision_count(self) -> int:
        return sum(len(items) for items in self._revisions.values())
