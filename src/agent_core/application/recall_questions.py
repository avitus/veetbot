"""The owner's recall question set over HTTP and the operator CLI (ADR-0339)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from uuid import UUID

from agent_core.application.authorization import require_scope
from agent_core.domain.agents import Principal
from agent_core.domain.errors import ConflictError, NotFoundError
from agent_core.domain.recall_questions import (
    MAX_QUESTIONS_PER_PRINCIPAL,
    QuestionImportOutcome,
    QuestionResult,
    QuestionSplit,
    QuestionStatus,
    RecallQuestion,
    RecallQuestionContent,
    RecallQuestionHistory,
    RecallQuestionList,
    RecallQuestionView,
)
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.persistence import RepositoryUnitOfWork, UnitOfWorkFactory
from agent_core.ports.recall_questions import QuestionKeyExists


class PublicRecallQuestionService:
    """Keep the question set: add, edit, retire and restore, never move a split.

    A write names the revision it read. Repeating a write that already took
    effect returns the stored question, so a retried request is safe; a write
    against a question changed since is a conflict.
    """

    def __init__(self, *, uow_factory: UnitOfWorkFactory, clock: Clock, ids: IdFactory) -> None:
        self._uow_factory = uow_factory
        self._clock = clock
        self._ids = ids

    async def list_questions(
        self,
        principal: Principal,
        *,
        split: QuestionSplit | None = None,
        status: QuestionStatus | None = None,
    ) -> RecallQuestionList:
        require_scope(principal, "memory.read")
        async with self._uow_factory() as uow:
            questions = await uow.recall_questions.list_questions(
                principal, split=split, status=status
            )
        return RecallQuestionList(items=tuple(RecallQuestionView.of(item) for item in questions))

    async def create(
        self,
        principal: Principal,
        *,
        key: str,
        split: QuestionSplit,
        content: RecallQuestionContent,
    ) -> RecallQuestionView:
        require_scope(principal, "memory.write")
        async with self._uow_factory() as uow:
            return RecallQuestionView.of(await self._create(uow, principal, key, split, content))

    async def edit(
        self,
        principal: Principal,
        question_id: UUID,
        *,
        expected_revision: int,
        content: RecallQuestionContent,
    ) -> RecallQuestionView:
        require_scope(principal, "memory.write")
        async with self._uow_factory() as uow:
            stored = await self._owned(uow, principal, question_id)
            if stored.content == content:
                return RecallQuestionView.of(stored)
            return RecallQuestionView.of(
                await self._save(uow, stored, expected_revision, content=content)
            )

    async def set_status(
        self,
        principal: Principal,
        question_id: UUID,
        *,
        expected_revision: int,
        status: QuestionStatus,
    ) -> RecallQuestionView:
        require_scope(principal, "memory.write")
        async with self._uow_factory() as uow:
            stored = await self._owned(uow, principal, question_id)
            if stored.status is status:
                return RecallQuestionView.of(stored)
            return RecallQuestionView.of(
                await self._save(uow, stored, expected_revision, status=status)
            )

    async def history(self, principal: Principal, question_id: UUID) -> RecallQuestionHistory:
        require_scope(principal, "memory.read")
        async with self._uow_factory() as uow:
            await self._owned(uow, principal, question_id)
            return RecallQuestionHistory(
                items=tuple(await uow.recall_questions.history(principal, question_id))
            )

    async def stored_questions(
        self, principal: Principal, *, split: QuestionSplit | None = None
    ) -> list[RecallQuestion]:
        """The active questions as stored, for the probe tool and for export."""

        require_scope(principal, "memory.read")
        async with self._uow_factory() as uow:
            return await uow.recall_questions.list_questions(
                principal, split=split, status=QuestionStatus.ACTIVE
            )

    async def import_questions(
        self,
        principal: Principal,
        *,
        split: QuestionSplit,
        items: Sequence[tuple[str, RecallQuestionContent]],
    ) -> QuestionImportOutcome:
        """Add or update questions by key in one transaction; all of them or none.

        An existing question keeps its split: importing its key into the other
        half is refused, because that would move it.
        """

        require_scope(principal, "memory.write")
        keys = [key for key, _content in items]
        if len(set(keys)) != len(keys):
            raise ConflictError("the import names a question key twice")
        created = updated = unchanged = 0
        async with self._uow_factory() as uow:
            for key, content in items:
                stored = await uow.recall_questions.get_by_key(principal, key)
                if stored is None:
                    await self._create(uow, principal, key, split, content)
                    created += 1
                elif stored.split is not split:
                    raise ConflictError(
                        f"question {key} is in the {stored.split.value} half and cannot move"
                    )
                elif stored.content == content and stored.status is QuestionStatus.ACTIVE:
                    unchanged += 1
                else:
                    await self._save(
                        uow,
                        stored,
                        stored.revision,
                        content=content,
                        status=QuestionStatus.ACTIVE,
                    )
                    updated += 1
        return QuestionImportOutcome(created=created, updated=updated, unchanged=unchanged)

    async def record_results(
        self, principal: Principal, results: Mapping[str, QuestionResult]
    ) -> int:
        """Set each named question's latest run result; unknown keys are skipped."""

        require_scope(principal, "memory.write")
        recorded = 0
        async with self._uow_factory() as uow:
            for key, result in results.items():
                recorded += await uow.recall_questions.record_result(principal, key, result)
        return recorded

    async def _owned(
        self, uow: RepositoryUnitOfWork, principal: Principal, question_id: UUID
    ) -> RecallQuestion:
        stored = await uow.recall_questions.get(principal, question_id)
        if stored is None:
            raise NotFoundError("recall question not found")
        return stored

    async def _create(
        self,
        uow: RepositoryUnitOfWork,
        principal: Principal,
        key: str,
        split: QuestionSplit,
        content: RecallQuestionContent,
    ) -> RecallQuestion:
        existing = await uow.recall_questions.get_by_key(principal, key)
        if existing is not None:
            if (existing.split, existing.content) == (split, content):
                return existing
            raise ConflictError(f"a different question already has the key {key}")
        if await uow.recall_questions.count(principal) >= MAX_QUESTIONS_PER_PRINCIPAL:
            raise ConflictError("the question set is full")
        now = self._clock.now()
        question = RecallQuestion(
            id=self._ids.new_id(),
            tenant_id=principal.tenant_id,
            principal_id=principal.principal_id,
            key=key,
            split=split,
            content=content,
            created_at=now,
            updated_at=now,
        )
        try:
            await uow.recall_questions.create(question)
        except QuestionKeyExists as error:
            raise ConflictError(f"a question already has the key {key}") from error
        return question

    async def _save(
        self,
        uow: RepositoryUnitOfWork,
        stored: RecallQuestion,
        expected_revision: int,
        *,
        content: RecallQuestionContent | None = None,
        status: QuestionStatus | None = None,
    ) -> RecallQuestion:
        if expected_revision != stored.revision:
            raise ConflictError(
                f"recall question expected revision {expected_revision} "
                f"but it is at {stored.revision}"
            )
        saved = stored.model_copy(
            update={
                "content": stored.content if content is None else content,
                "status": stored.status if status is None else status,
                "revision": stored.revision + 1,
                "updated_at": self._clock.now(),
            }
        )
        if not await uow.recall_questions.save(saved, expected_revision=expected_revision):
            raise ConflictError("recall question changed while it was being saved")
        return saved
