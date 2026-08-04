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
                .order_by(SkillRow.name, SkillRevisionRow.revision.desc())
            )
        ).all()
        latest: dict[str, SkillRevision] = {}
        for identity, revision in rows:
            latest.setdefault(identity.name, self._to_domain(identity, revision))
        return list(latest.values())[:limit]

    async def archive(self, tenant_id: str, name: str, revision: int) -> None:
        identity_id = await self._session.scalar(
            select(SkillRow.id).where(SkillRow.tenant_id == tenant_id, SkillRow.name == name)
        )
        if identity_id is None:
            raise NotFoundError("skill revision not found")
        result = await self._session.execute(
            update(SkillRevisionRow)
            .where(
                SkillRevisionRow.skill_id == identity_id,
                SkillRevisionRow.revision == revision,
            )
            .values(status=SkillStatus.ARCHIVED.value)
        )
        if not int(getattr(result, "rowcount", 0) or 0):
            raise NotFoundError("skill revision not found")

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
            created_at=row.created_at,
        )
