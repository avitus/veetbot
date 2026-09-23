"""PostgreSQL owner model-settings store (ADR-0119)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.functions import func

from agent_core.adapters.persistence.sqlalchemy_models import ModelSettingsRow
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError
from agent_core.domain.messages import ReasoningEffort
from agent_core.domain.model_settings import ModelChoice, ModelSettings


def _effort(value: str | None) -> ReasoningEffort | None:
    return None if value is None else ReasoningEffort(value)


def _settings_from_row(row: ModelSettingsRow) -> ModelSettings:
    return ModelSettings(
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        version=row.version,
        chat=ModelChoice(
            model_policy=row.chat_model_policy,
            reasoning_effort=_effort(row.chat_reasoning_effort),
        ),
        memory=ModelChoice(
            model_policy=row.memory_model_policy,
            reasoning_effort=_effort(row.memory_reasoning_effort),
        ),
        created_at=row.created_at,
    )


class PostgresModelSettingsStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def current(self, principal: Principal) -> ModelSettings | None:
        row = (
            await self._session.execute(
                select(ModelSettingsRow)
                .where(
                    ModelSettingsRow.tenant_id == principal.tenant_id,
                    ModelSettingsRow.principal_id == principal.principal_id,
                )
                .order_by(ModelSettingsRow.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return None if row is None else _settings_from_row(row)

    async def append_version(
        self, settings: ModelSettings, *, expected_version: int
    ) -> ModelSettings:
        head = (
            await self._session.execute(
                select(func.max(ModelSettingsRow.version)).where(
                    ModelSettingsRow.tenant_id == settings.tenant_id,
                    ModelSettingsRow.principal_id == settings.principal_id,
                )
            )
        ).scalar_one()
        current = int(head) if head is not None else 0
        if expected_version != current:
            raise ConflictError(
                f"model settings expected version {expected_version} but head is {current}"
            )
        if settings.version != current + 1:
            raise ConflictError(
                f"model settings version {settings.version} does not follow head {current}"
            )
        try:
            await self._session.execute(
                pg_insert(ModelSettingsRow).values(
                    tenant_id=settings.tenant_id,
                    principal_id=settings.principal_id,
                    version=settings.version,
                    chat_model_policy=settings.chat.model_policy,
                    chat_reasoning_effort=(
                        None
                        if settings.chat.reasoning_effort is None
                        else settings.chat.reasoning_effort.value
                    ),
                    memory_model_policy=settings.memory.model_policy,
                    memory_reasoning_effort=(
                        None
                        if settings.memory.reasoning_effort is None
                        else settings.memory.reasoning_effort.value
                    ),
                    created_at=settings.created_at,
                )
            )
        except IntegrityError as error:
            raise ConflictError(
                f"model settings version {settings.version} was written concurrently"
            ) from error
        return settings
