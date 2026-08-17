"""PostgreSQL skill identities and immutable revisions."""

from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.sqlalchemy_models import SkillRevisionRow, SkillRow
from agent_core.domain.errors import ConflictError, NotFoundError, SkillRevisionConflict
from agent_core.domain.policies import TrustLevel
from agent_core.domain.skills import (
    AuthoringContext,
    SkillManifest,
    SkillPackage,
    SkillRef,
    SkillRevision,
    SkillSource,
    SkillStatus,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import TransactionCallbackRegistrar
from agent_core.ports.skills import SkillPackageStore
from agent_core.skills.package import SkillPackageValidator


class PostgresSkillRepository:
    def __init__(
        self,
        session: AsyncSession,
        store: SkillPackageStore,
        validator: SkillPackageValidator,
        clock: Clock,
        ids: IdFactory,
        register_rollback: TransactionCallbackRegistrar,
    ) -> None:
        self._session = session
        self._store = store
        self._validator = validator
        self._clock = clock
        self._ids = ids
        self._register_rollback = register_rollback

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
        if authored_by is not None:
            prior = (
                await self._session.execute(
                    select(SkillRow, SkillRevisionRow)
                    .join(SkillRevisionRow, SkillRevisionRow.skill_id == SkillRow.id)
                    .where(
                        SkillRow.tenant_id == tenant_id,
                        SkillRevisionRow.authored_by_invocation_id == authored_by.invocation_id,
                    )
                )
            ).one_or_none()
            if prior is not None:
                if prior[1].authoring_idempotency_key != authored_by.idempotency_key:
                    raise ConflictError(
                        "skill authoring invocation was reused with different arguments",
                        reason="skill_authoring_idempotency_conflict",
                    )
                return self._to_domain(prior[0], prior[1])
        await self._session.execute(
            pg_insert(SkillRow)
            .values(
                id=self._ids.new_id(),
                tenant_id=tenant_id,
                name=validated.manifest.name,
                source=source.value,
                created_at=self._clock.now(),
            )
            .on_conflict_do_nothing(constraint="uq_skills_tenant_name")
        )
        identity = (
            await self._session.scalars(
                select(SkillRow)
                .where(
                    SkillRow.tenant_id == tenant_id,
                    SkillRow.name == validated.manifest.name,
                )
                .with_for_update()
            )
        ).one()
        if identity.source != source.value:
            raise ConflictError("skill source cannot change across revisions")
        if authored_by is not None:
            locked_prior = (
                await self._session.scalars(
                    select(SkillRevisionRow).where(
                        SkillRevisionRow.authored_by_invocation_id == authored_by.invocation_id
                    )
                )
            ).one_or_none()
            if locked_prior is not None:
                if (
                    locked_prior.skill_id != identity.id
                    or locked_prior.authoring_idempotency_key != authored_by.idempotency_key
                ):
                    raise ConflictError(
                        "skill authoring invocation was reused with different arguments",
                        reason="skill_authoring_idempotency_conflict",
                    )
                return self._to_domain(identity, locked_prior)
        latest = (
            await self._session.scalars(
                select(SkillRevisionRow)
                .where(SkillRevisionRow.skill_id == identity.id)
                .order_by(SkillRevisionRow.revision.desc())
                .limit(1)
            )
        ).one_or_none()
        current_revision = 0 if latest is None else latest.revision
        if expected_revision is not None and current_revision != expected_revision:
            raise SkillRevisionConflict(current_revision)
        revision_number = current_revision + 1
        stored = await self._store.put(tenant_id, identity.id, revision_number, validated.archive)
        if stored.created:
            self._register_rollback(lambda: self._store.delete(stored.key))
        trust = (
            TrustLevel.TRUSTED_CONFIGURATION
            if source in {SkillSource.BUILTIN, SkillSource.OPERATOR}
            else TrustLevel.EXTERNAL_UNTRUSTED
        )
        revision_id = self._ids.new_id()
        await self._session.execute(
            pg_insert(SkillRevisionRow).values(
                id=revision_id,
                skill_id=identity.id,
                revision=revision_number,
                version=validated.manifest.version,
                description=validated.manifest.description,
                required_tools=list(validated.manifest.required_tools),
                body=validated.body,
                body_tokens=validated.body_tokens,
                content_sha256=validated.content_sha256,
                package_key=stored.key,
                package_bytes=validated.package_bytes,
                file_count=validated.file_count,
                trust=trust.value,
                status=SkillStatus.ACTIVE.value,
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
        )
        row = (
            await self._session.scalars(
                select(SkillRevisionRow).where(SkillRevisionRow.id == revision_id)
            )
        ).one()
        return self._to_domain(identity, row)

    async def resolve(self, tenant_id: str, ref: SkillRef) -> SkillRevision:
        statement = (
            select(SkillRow, SkillRevisionRow)
            .join(SkillRevisionRow, SkillRevisionRow.skill_id == SkillRow.id)
            .where(SkillRow.tenant_id == tenant_id, SkillRow.name == ref.name)
        )
        if ref.revision is None:
            statement = statement.where(
                SkillRevisionRow.status == SkillStatus.ACTIVE.value
            ).order_by(SkillRevisionRow.revision.desc())
        else:
            statement = statement.where(SkillRevisionRow.revision == ref.revision)
        found = (await self._session.execute(statement.limit(1))).one_or_none()
        if found is None:
            raise NotFoundError(f"skill reference {ref} was not found")
        return self._to_domain(found[0], found[1])

    async def list_active(self, tenant_id: str, limit: int) -> list[SkillRevision]:
        if limit <= 0:
            return []
        rows = (
            await self._session.execute(
                select(SkillRow, SkillRevisionRow)
                .join(SkillRevisionRow, SkillRevisionRow.skill_id == SkillRow.id)
                .where(
                    SkillRow.tenant_id == tenant_id,
                    SkillRevisionRow.status == SkillStatus.ACTIVE.value,
                )
                .distinct(SkillRow.name)
                .order_by(SkillRow.name, SkillRevisionRow.revision.desc())
                .limit(limit)
            )
        ).all()
        return [self._to_domain(identity, revision) for identity, revision in rows]

    async def archive(
        self,
        tenant_id: str,
        name: str,
        revision: int,
        authored_by: AuthoringContext | None = None,
    ) -> SkillRevision:
        if authored_by is not None:
            prior = (
                await self._session.execute(
                    select(SkillRow, SkillRevisionRow)
                    .join(SkillRevisionRow, SkillRevisionRow.skill_id == SkillRow.id)
                    .where(
                        SkillRow.tenant_id == tenant_id,
                        SkillRevisionRow.archived_by_invocation_id == authored_by.invocation_id,
                    )
                )
            ).one_or_none()
            if prior is not None:
                if (
                    prior[0].name != name
                    or prior[1].revision != revision
                    or prior[1].archive_idempotency_key != authored_by.idempotency_key
                ):
                    raise ConflictError(
                        "skill archive invocation was reused with different arguments",
                        reason="skill_authoring_idempotency_conflict",
                    )
                return self._to_domain(prior[0], prior[1])
        identity = await self._session.scalar(
            select(SkillRow)
            .where(SkillRow.tenant_id == tenant_id, SkillRow.name == name)
            .with_for_update()
        )
        if identity is None:
            raise NotFoundError("skill revision not found")
        if authored_by is not None:
            locked_prior = (
                await self._session.scalars(
                    select(SkillRevisionRow).where(
                        SkillRevisionRow.archived_by_invocation_id == authored_by.invocation_id
                    )
                )
            ).one_or_none()
            if locked_prior is not None:
                if (
                    locked_prior.skill_id != identity.id
                    or locked_prior.revision != revision
                    or locked_prior.archive_idempotency_key != authored_by.idempotency_key
                ):
                    raise ConflictError(
                        "skill archive invocation was reused with different arguments",
                        reason="skill_authoring_idempotency_conflict",
                    )
                return self._to_domain(identity, locked_prior)
        current_revision = await self._session.scalar(
            select(SkillRevisionRow.revision)
            .where(
                SkillRevisionRow.skill_id == identity.id,
                SkillRevisionRow.status == SkillStatus.ACTIVE.value,
            )
            .order_by(SkillRevisionRow.revision.desc())
            .limit(1)
        )
        if current_revision is None:
            raise NotFoundError("skill revision not found")
        if current_revision != revision:
            raise SkillRevisionConflict(current_revision)
        result = await self._session.execute(
            update(SkillRevisionRow)
            .where(
                SkillRevisionRow.skill_id == identity.id,
                SkillRevisionRow.revision == revision,
            )
            .values(
                status=SkillStatus.ARCHIVED.value,
                archived_by_invocation_id=(
                    None if authored_by is None else authored_by.invocation_id
                ),
                archive_idempotency_key=(
                    None if authored_by is None else authored_by.idempotency_key
                ),
            )
            .returning(SkillRevisionRow)
        )
        archived = result.scalar_one_or_none()
        if archived is None:
            raise NotFoundError("skill revision not found")
        return self._to_domain(identity, archived)

    @staticmethod
    def _to_domain(identity: SkillRow, row: SkillRevisionRow) -> SkillRevision:
        return SkillRevision(
            skill_id=identity.id,
            tenant_id=identity.tenant_id,
            revision=row.revision,
            manifest=SkillManifest(
                name=identity.name,
                version=row.version,
                description=row.description,
                required_tools=tuple(row.required_tools),
            ),
            body=row.body,
            body_tokens=row.body_tokens,
            content_sha256=row.content_sha256,
            package_key=row.package_key,
            package_bytes=row.package_bytes,
            file_count=row.file_count,
            source=SkillSource(identity.source),
            trust=TrustLevel(row.trust),
            status=SkillStatus(row.status),
            authored_by_run_id=row.authored_by_run_id,
            authored_by_principal_id=row.authored_by_principal_id,
            authored_by_invocation_id=row.authored_by_invocation_id,
            authoring_idempotency_key=row.authoring_idempotency_key,
            archived_by_invocation_id=row.archived_by_invocation_id,
            archive_idempotency_key=row.archive_idempotency_key,
            created_at=row.created_at,
        )
