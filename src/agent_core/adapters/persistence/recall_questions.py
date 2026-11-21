"""Recall question storage: the in-memory adapter and the PostgreSQL adapter."""

import asyncio
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from functools import partial
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, delete, func, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent_core.adapters.memory.transactions import MemoryTransaction
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
    """Contract-backed recall question storage.

    A question is identified by its owner and its identifier together, as in
    PostgreSQL. Given the memory transaction, every write is journaled and the
    owner's lock is held until the unit of work ends, as the memory store does:
    a failed unit of work leaves the questions as they were, and no other unit
    of work can read or build on a write that may still be undone.
    """

    def __init__(self, transaction: MemoryTransaction | None = None) -> None:
        self._questions: dict[tuple[str, str], dict[UUID, RecallQuestion]] = {}
        self._history: dict[tuple[str, str, UUID], list[RecallQuestionRevision]] = {}
        self._transaction = transaction
        self._lock = asyncio.Lock()

    def _guard(self, scope: tuple[str, str]) -> AbstractAsyncContextManager[None]:
        if self._transaction is not None:
            return self._transaction.owner(scope)
        return self._lock

    def _undo(self, undo: Callable[[], object]) -> None:
        if self._transaction is not None:
            self._transaction.remember(undo)

    def _put(self, owned: dict[UUID, RecallQuestion], question: RecallQuestion) -> None:
        previous = owned.get(question.id)
        if previous is None:
            self._undo(lambda: owned.pop(question.id, None))
        else:
            self._undo(lambda: owned.__setitem__(question.id, previous))
        owned[question.id] = question

    async def create(self, question: RecallQuestion) -> None:
        scope = (question.tenant_id, question.principal_id)
        async with self._guard(scope):
            owned = self._questions.setdefault(scope, {})
            if question.id in owned or any(item.key == question.key for item in owned.values()):
                raise QuestionKeyExists(question.key)
            self._put(owned, question)
            history_key = (*scope, question.id)
            self._undo(lambda: self._history.pop(history_key, None))
            self._history[history_key] = [_revision(question)]

    async def get(self, principal: Principal, question_id: UUID) -> RecallQuestion | None:
        scope = (principal.tenant_id, principal.principal_id)
        async with self._guard(scope):
            return self._questions.get(scope, {}).get(question_id)

    async def get_by_key(self, principal: Principal, key: str) -> RecallQuestion | None:
        scope = (principal.tenant_id, principal.principal_id)
        async with self._guard(scope):
            owned = self._questions.get(scope, {})
            return next((item for item in owned.values() if item.key == key), None)

    async def list_questions(
        self,
        principal: Principal,
        *,
        split: QuestionSplit | None = None,
        status: QuestionStatus | None = None,
    ) -> list[RecallQuestion]:
        scope = (principal.tenant_id, principal.principal_id)
        async with self._guard(scope):
            return sorted(
                (
                    item
                    for item in self._questions.get(scope, {}).values()
                    if (split is None or item.split is split)
                    and (status is None or item.status is status)
                ),
                key=lambda item: item.key,
            )

    async def count(self, principal: Principal) -> int:
        scope = (principal.tenant_id, principal.principal_id)
        async with self._guard(scope):
            return len(self._questions.get(scope, {}))

    async def save(
        self, question: RecallQuestion, *, expected_revision: int, clear_result: bool = False
    ) -> bool:
        scope = (question.tenant_id, question.principal_id)
        async with self._guard(scope):
            owned = self._questions.get(scope, {})
            stored = owned.get(question.id)
            if stored is None or stored.revision != expected_revision:
                return False
            saved = stored.model_copy(
                update={
                    "status": question.status,
                    "content": question.content,
                    "revision": question.revision,
                    "latest_result": None if clear_result else stored.latest_result,
                    "updated_at": question.updated_at,
                }
            )
            self._put(owned, saved)
            history = self._history[(*scope, question.id)]
            entry = _revision(saved)
            self._undo(lambda: history.remove(entry))
            history.append(entry)
            return True

    async def history(
        self, principal: Principal, question_id: UUID
    ) -> list[RecallQuestionRevision]:
        scope = (principal.tenant_id, principal.principal_id)
        async with self._guard(scope):
            if question_id not in self._questions.get(scope, {}):
                return []
            return list(self._history[(*scope, question_id)])

    async def record_result(
        self, principal: Principal, key: str, result: QuestionResult, *, expected_revision: int
    ) -> bool:
        scope = (principal.tenant_id, principal.principal_id)
        async with self._guard(scope):
            owned = self._questions.get(scope, {})
            stored = next((item for item in owned.values() if item.key == key), None)
            if stored is None or stored.revision != expected_revision:
                return False
            self._put(owned, stored.model_copy(update={"latest_result": result}))
            return True

    async def erase_principal(self, principal: Principal) -> int:
        scope = (principal.tenant_id, principal.principal_id)
        async with self._guard(scope):
            owned = self._questions.pop(scope, None)
            if owned is None:
                return 0
            self._undo(lambda: self._questions.__setitem__(scope, owned))
            for question_id in owned:
                history_key = (*scope, question_id)
                history = self._history.pop(history_key, None)
                if history is not None:
                    self._undo(partial(self._history.__setitem__, history_key, history))
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

    async def save(
        self, question: RecallQuestion, *, expected_revision: int, clear_result: bool = False
    ) -> bool:
        owner = Principal(tenant_id=question.tenant_id, principal_id=question.principal_id)
        values: dict[str, Any] = {
            "status": question.status.value,
            "content": question.content.model_dump(mode="json"),
            "revision": question.revision,
            "updated_at": question.updated_at,
        }
        if clear_result:
            # Otherwise the column is not named, so a run recorded since the
            # caller's read is kept.
            values["latest_result"] = None
        result = await self._session.execute(
            update(RecallQuestionRow)
            .where(
                *self._scope(owner),
                RecallQuestionRow.id == question.id,
                RecallQuestionRow.revision == expected_revision,
            )
            .values(**values)
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

    async def record_result(
        self, principal: Principal, key: str, result: QuestionResult, *, expected_revision: int
    ) -> bool:
        outcome = await self._session.execute(
            update(RecallQuestionRow)
            .where(
                *self._scope(principal),
                RecallQuestionRow.key == key,
                RecallQuestionRow.revision == expected_revision,
            )
            .values(latest_result=result.model_dump(mode="json"))
        )
        return bool(int(getattr(outcome, "rowcount", 0) or 0))

    async def erase_principal(self, principal: Principal) -> int:
        outcome = await self._session.execute(
            delete(RecallQuestionRow).where(*self._scope(principal))
        )
        return int(getattr(outcome, "rowcount", 0) or 0)
