"""Owner-isolated storage for the recall question set (ADR-0339)."""

from typing import Protocol
from uuid import UUID

from agent_core.domain.agents import Principal
from agent_core.domain.recall_questions import (
    QuestionResult,
    QuestionSplit,
    QuestionStatus,
    RecallQuestion,
    RecallQuestionRevision,
)

__all__ = ["QuestionKeyExists", "RecallQuestionStore"]


class QuestionKeyExists(Exception):  # noqa: N818
    """The owner already has a question with that key."""


class RecallQuestionStore(Protocol):
    async def create(self, question: RecallQuestion) -> None:
        """Store a new question and its first history row, or raise `QuestionKeyExists`."""
        ...

    async def get(self, principal: Principal, question_id: UUID) -> RecallQuestion | None: ...

    async def get_by_key(self, principal: Principal, key: str) -> RecallQuestion | None: ...

    async def list_questions(
        self,
        principal: Principal,
        *,
        split: QuestionSplit | None = None,
        status: QuestionStatus | None = None,
    ) -> list[RecallQuestion]:
        """The owner's questions in key order."""
        ...

    async def count(self, principal: Principal) -> int: ...

    async def save(self, question: RecallQuestion, *, expected_revision: int) -> bool:
        """Replace content, status and latest result when the revision is the expected one.

        Returns False, changing nothing, when the question is missing or was
        changed since. The key and the split are never rewritten.
        """
        ...

    async def history(
        self, principal: Principal, question_id: UUID
    ) -> list[RecallQuestionRevision]:
        """Every state the question has held, oldest first."""
        ...

    async def record_result(
        self, principal: Principal, key: str, result: QuestionResult, *, expected_revision: int
    ) -> bool:
        """Set the latest run result of the question with that key; no revision moves.

        Returns False, changing nothing, when the owner has no such question or
        it has left the revision the caller compared the result with.
        """
        ...

    async def erase_principal(self, principal: Principal) -> int: ...
