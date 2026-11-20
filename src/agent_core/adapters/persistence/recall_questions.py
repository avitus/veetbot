"""Recall question storage: the in-memory adapter and the PostgreSQL adapter."""

import asyncio
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.persistence.sqlalchemy_models import (
    RecallQuestionRevisionRow,
    RecallQuestionRow,
)
from agent_core.domain.agents import Principal
from agent_core.domain.recall_questions import (
    QuestionResult,
    QuestionSplit,
    QuestionStatus,
    RecallQuestion,
    RecallQuestionContent,
    RecallQuestionRevision,
)
from agent_core.ports.recall_questions import QuestionKeyExists


def _revision(question: RecallQuestion) -> RecallQuestionRevision:
    return RecallQuestionRevision(
        question_id=question.id,
        revision=question.revision,
        status=question.status,
        content=question.content,
        changed_at=question.updated_at,
    )


class InMemoryRecallQuestionStore:
    """Contract-backed recall question storage."""

    def __init__(self) -> None:
        self._questions: dict[tuple[str, str], dict[UUID, RecallQuestion]] = {}
        self._history: dict[UUID, list[RecallQuestionRevision]] = {}
        self._lock = asyncio.Lock()

    def _owned(self, principal: Principal) -> dict[UUID, RecallQuestion]:
        return self._questions.get((principal.tenant_id, principal.principal_id), {})

    async def create(self, question: RecallQuestion) -> None:
        async with self._lock:
            owned = self._questions.setdefault((question.tenant_id, question.principal_id), {})
            if question.id in owned or any(item.key == question.key for item in owned.values()):
                raise QuestionKeyExists(question.key)
            owned[question.id] = question
            self._history[question.id] = [_revision(question)]

    async def get(self, principal: Principal, question_id: UUID) -> RecallQuestion | None:
        return self._owned(principal).get(question_id)

    async def get_by_key(self, principal: Principal, key: str) -> RecallQuestion | None:
        return next((item for item in self._owned(principal).values() if item.key == key), None)

    async def list_questions(
        self,
        principal: Principal,
        *,
        split: QuestionSplit | None = None,
        status: QuestionStatus | None = None,
    ) -> list[RecallQuestion]:
        return sorted(
            (
                item
                for item in self._owned(principal).values()
                if (split is None or item.split is split)
                and (status is None or item.status is status)
            ),
            key=lambda item: item.key,
        )

    async def count(self, principal: Principal) -> int:
        return len(self._owned(principal))

    async def save(self, question: RecallQuestion, *, expected_revision: int) -> bool:
        async with self._lock:
            owned = self._questions.get((question.tenant_id, question.principal_id), {})
            stored = owned.get(question.id)
            if stored is None or stored.revision != expected_revision:
                return False
            saved = stored.model_copy(
                update={
                    "status": question.status,
                    "content": question.content,
                    "revision": question.revision,
                    "updated_at": question.updated_at,
                }
            )
            owned[question.id] = saved
            self._history[question.id].append(_revision(saved))
            return True

    async def history(
        self, principal: Principal, question_id: UUID
    ) -> list[RecallQuestionRevision]:
        if question_id not in self._owned(principal):
            return []
        return list(self._history[question_id])

    async def record_result(self, principal: Principal, key: str, result: QuestionResult) -> bool:
        async with self._lock:
            owned = self._questions.get((principal.tenant_id, principal.principal_id), {})
            stored = next((item for item in owned.values() if item.key == key), None)
            if stored is None:
                return False
            owned[stored.id] = stored.model_copy(update={"latest_result": result})
            return True

    async def erase_principal(self, principal: Principal) -> int:
        async with self._lock:
            owned = self._questions.pop((principal.tenant_id, principal.principal_id), {})
            for question_id in owned:
                self._history.pop(question_id, None)
            return len(owned)


def _question_from_row(row: RecallQuestionRow) -> RecallQuestion:
    return RecallQuestion(
        id=row.id,
        tenant_id=row.tenant_id,
        principal_id=row.principal_id,
        key=row.key,
        split=QuestionSplit(row.split),
        status=QuestionStatus(row.status),
        content=RecallQuestionContent.model_validate(row.content),
        revision=row.revision,
        latest_result=(
            None if row.latest_result is None else QuestionResult.model_validate(row.latest_result)
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _revision_values(question: RecallQuestion) -> dict[str, Any]:
    return {
        "tenant_id": question.tenant_id,
        "principal_id": question.principal_id,
        "question_id": question.id,
        "revision": question.revision,
        "status": question.status.value,
        "content": question.content.model_dump(mode="json"),
        "changed_at": question.updated_at,
    }


class PostgresRecallQuestionStore:
    """Recall questions in PostgreSQL; the unit of work owns the transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def _scope(principal: Principal) -> list[ColumnElement[bool]]:
        return [
            RecallQuestionRow.tenant_id == principal.tenant_id,
            RecallQuestionRow.principal_id == principal.principal_id,
        ]

    async def create(self, question: RecallQuestion) -> None:
        owner = Principal(tenant_id=question.tenant_id, principal_id=question.principal_id)
        if await self.get_by_key(owner, question.key) is not None:
            raise QuestionKeyExists(question.key)
        try:
            # A savepoint keeps a lost race from aborting the caller's transaction.
            async with self._session.begin_nested():
                await self._session.execute(
                    insert(RecallQuestionRow).values(
                        tenant_id=question.tenant_id,
                        principal_id=question.principal_id,
                        id=question.id,
                        key=question.key,
                        split=question.split.value,
                        status=question.status.value,
                        content=question.content.model_dump(mode="json"),
                        revision=question.revision,
                        latest_result=(
                            None
                            if question.latest_result is None
                            else question.latest_result.model_dump(mode="json")
                        ),
                        created_at=question.created_at,
                        updated_at=question.updated_at,
                    )
                )
                await self._session.execute(
                    insert(RecallQuestionRevisionRow).values(**_revision_values(question))
                )
        except IntegrityError as error:
            raise QuestionKeyExists(question.key) from error

    async def get(self, principal: Principal, question_id: UUID) -> RecallQuestion | None:
        row = (
            await self._session.execute(
                select(RecallQuestionRow).where(
                    *self._scope(principal), RecallQuestionRow.id == question_id
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _question_from_row(row)

    async def get_by_key(self, principal: Principal, key: str) -> RecallQuestion | None:
        row = (
            await self._session.execute(
                select(RecallQuestionRow).where(
                    *self._scope(principal), RecallQuestionRow.key == key
                )
            )
        ).scalar_one_or_none()
        return None if row is None else _question_from_row(row)

    async def list_questions(
        self,
        principal: Principal,
        *,
        split: QuestionSplit | None = None,
        status: QuestionStatus | None = None,
    ) -> list[RecallQuestion]:
        statement = select(RecallQuestionRow).where(*self._scope(principal))
        if split is not None:
            statement = statement.where(RecallQuestionRow.split == split.value)
        if status is not None:
            statement = statement.where(RecallQuestionRow.status == status.value)
        rows = (await self._session.execute(statement.order_by(RecallQuestionRow.key))).scalars()
        return [_question_from_row(row) for row in rows]

    async def count(self, principal: Principal) -> int:
        return int(
            (
                await self._session.execute(
                    select(func.count())
                    .select_from(RecallQuestionRow)
                    .where(*self._scope(principal))
                )
            ).scalar_one()
        )

    async def save(self, question: RecallQuestion, *, expected_revision: int) -> bool:
        owner = Principal(tenant_id=question.tenant_id, principal_id=question.principal_id)
        result = await self._session.execute(
            update(RecallQuestionRow)
            .where(
                *self._scope(owner),
                RecallQuestionRow.id == question.id,
                RecallQuestionRow.revision == expected_revision,
            )
            .values(
                status=question.status.value,
                content=question.content.model_dump(mode="json"),
                revision=question.revision,
                updated_at=question.updated_at,
            )
        )
        if not int(getattr(result, "rowcount", 0) or 0):
            return False
        await self._session.execute(
            insert(RecallQuestionRevisionRow).values(**_revision_values(question))
        )
        return True

    async def history(
        self, principal: Principal, question_id: UUID
    ) -> list[RecallQuestionRevision]:
        rows = (
            await self._session.execute(
                select(RecallQuestionRevisionRow)
                .where(
                    RecallQuestionRevisionRow.tenant_id == principal.tenant_id,
                    RecallQuestionRevisionRow.principal_id == principal.principal_id,
                    RecallQuestionRevisionRow.question_id == question_id,
                )
                .order_by(RecallQuestionRevisionRow.revision)
            )
        ).scalars()
        return [
            RecallQuestionRevision(
                question_id=row.question_id,
                revision=row.revision,
                status=QuestionStatus(row.status),
                content=RecallQuestionContent.model_validate(row.content),
                changed_at=row.changed_at,
            )
            for row in rows
        ]

    async def record_result(self, principal: Principal, key: str, result: QuestionResult) -> bool:
        outcome = await self._session.execute(
            update(RecallQuestionRow)
            .where(*self._scope(principal), RecallQuestionRow.key == key)
            .values(latest_result=result.model_dump(mode="json"))
        )
        return bool(int(getattr(outcome, "rowcount", 0) or 0))

    async def erase_principal(self, principal: Principal) -> int:
        outcome = await self._session.execute(
            delete(RecallQuestionRow).where(*self._scope(principal))
        )
        return int(getattr(outcome, "rowcount", 0) or 0)
