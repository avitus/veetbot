"""Owner recall questions: the standing question set a recall change is measured against.

A question is a turn the owner really asks, with the stored facts it should
reach. The set is split once into a development half a change may be tuned
against and a held-out half it may not, so a question's split is fixed when it
is created and no edit can move it (ADR-0338, ADR-0339).
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# The families of `agent memory probe`; a test holds the two sets equal.
RECALL_QUESTION_FAMILIES: Final = frozenset(
    {
        "attributes",
        "preferences",
        "relationships",
        "goals",
        "decisions",
        "routines",
        "temporal",
        "complex",
    }
)
MAX_QUESTIONS_PER_PRINCIPAL: Final = 1000
QUESTION_KEY_PATTERN: Final = r"^[a-z0-9][a-z0-9-]*$"
_TEXT_MAX: Final = 500
_LIST_MAX: Final = 32


class QuestionSplit(StrEnum):
    DEVELOPMENT = "development"
    HOLDOUT = "holdout"


class QuestionStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class QuestionEvidence(BaseModel):
    """One stored fact the question should reach: a belief, or text one contains."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    belief_id: UUID | None = None
    statement_contains: str | None = Field(default=None, min_length=1, max_length=_TEXT_MAX)
    required: bool = True

    @model_validator(mode="after")
    def one_locator(self) -> QuestionEvidence:
        if (self.belief_id is None) == (self.statement_contains is None):
            raise ValueError("evidence names exactly one of belief_id or statement_contains")
        if self.statement_contains is not None and not self.statement_contains.strip():
            raise ValueError("evidence text is blank")
        return self


class RecallQuestionContent(BaseModel):
    """Everything about a question the owner may edit. The split is not here."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    question: str = Field(min_length=1, max_length=2000)
    search_text: str | None = Field(default=None, min_length=1, max_length=2000)
    family: str
    expected: tuple[QuestionEvidence, ...] = Field(default=(), max_length=_LIST_MAX)
    forbidden: tuple[str, ...] = Field(default=(), max_length=_LIST_MAX)
    delivered_must_contain: tuple[str, ...] = Field(default=(), max_length=_LIST_MAX)
    answer_must_contain: tuple[str, ...] = Field(default=(), max_length=_LIST_MAX)
    answer_must_not_contain: tuple[str, ...] = Field(default=(), max_length=_LIST_MAX)
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def measurable(self) -> RecallQuestionContent:
        if self.family not in RECALL_QUESTION_FAMILIES:
            raise ValueError(f"unknown question family {self.family!r}")
        for name in (
            "forbidden",
            "delivered_must_contain",
            "answer_must_contain",
            "answer_must_not_contain",
        ):
            texts = getattr(self, name)
            if any(not text.strip() or len(text) > _TEXT_MAX for text in texts):
                raise ValueError(f"{name} holds a blank or overlong text")
        if not (self.expected or self.delivered_must_contain or self.answer_must_contain):
            # Without a fact to reach or a name to render, a run of the
            # question can neither pass nor fail.
            raise ValueError("the question measures nothing: name a fact or a text it must reach")
        return self

    @property
    def criteria_sha256(self) -> str:
        """A fingerprint of what a run of the question tests.

        A recorded result names the fingerprint it was measured against, so a
        result for an older wording is never shown beside a newer one. The
        notes explain the question and change nothing a run checks, so they
        are left out.
        """

        canonical = json.dumps(
            self.model_dump(mode="json", exclude={"notes"}),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class QuestionPathResult(BaseModel):
    """How one path of the latest run went for a question: stages and counts, no text."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    path: Literal["automatic", "deliberate"]
    # The furthest stage each expected fact reached, in the question's order.
    stages: tuple[str, ...] = Field(default=(), max_length=_LIST_MAX)
    required_total: int = Field(default=0, ge=0)
    required_delivered: int = Field(default=0, ge=0)
    forbidden_delivered: int = Field(default=0, ge=0)
    delivered_found: tuple[bool, ...] = Field(default=(), max_length=_LIST_MAX)
    answer_correct: bool | None = None

    @property
    def passed(self) -> bool:
        return (
            self.required_delivered >= self.required_total
            and self.forbidden_delivered == 0
            and all(self.delivered_found)
            and self.answer_correct is not False
        )


class QuestionResult(BaseModel):
    """The latest probe run's outcome for one question."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    recorded_at: AwareDatetime
    report_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The `criteria_sha256` of the question content the run tested.
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    paths: tuple[QuestionPathResult, ...] = Field(min_length=1, max_length=2)

    @property
    def passed(self) -> bool:
        return all(path.passed for path in self.paths)


class RecallQuestion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    tenant_id: str
    principal_id: str
    key: str = Field(min_length=1, max_length=80, pattern=QUESTION_KEY_PATTERN)
    split: QuestionSplit
    status: QuestionStatus = QuestionStatus.ACTIVE
    content: RecallQuestionContent
    revision: int = Field(default=1, ge=1)
    # Written by a recorded run, which moves no revision. An edit that changes
    # what the question measures clears it.
    latest_result: QuestionResult | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class RecallQuestionRevision(BaseModel):
    """One past state of a question; the history is every state it has held."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    question_id: UUID
    revision: int = Field(ge=1)
    status: QuestionStatus
    content: RecallQuestionContent
    changed_at: AwareDatetime


class RecallQuestionView(BaseModel):
    """A question as its owner sees it on the page."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    id: UUID
    key: str
    split: QuestionSplit
    status: QuestionStatus
    content: RecallQuestionContent
    revision: int
    latest_result: QuestionResult | None
    # Whether the latest recorded run passed; None until a run is recorded.
    passed: bool | None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @classmethod
    def of(cls, question: RecallQuestion) -> RecallQuestionView:
        result = question.latest_result
        if result is not None and result.content_sha256 != question.content.criteria_sha256:
            # A result measured against other content says nothing about this one.
            result = None
        return cls(
            id=question.id,
            key=question.key,
            split=question.split,
            status=question.status,
            content=question.content,
            revision=question.revision,
            latest_result=result,
            passed=None if result is None else result.passed,
            created_at=question.created_at,
            updated_at=question.updated_at,
        )


class RecallQuestionList(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    items: tuple[RecallQuestionView, ...]


class RecallQuestionHistory(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    items: tuple[RecallQuestionRevision, ...]


class QuestionImportOutcome(BaseModel):
    """What an import did, by count; an import never retires or deletes."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    created: int = Field(default=0, ge=0)
    updated: int = Field(default=0, ge=0)
    unchanged: int = Field(default=0, ge=0)


class QuestionRecordOutcome(BaseModel):
    """What recording a run did, by count.

    A stale result was measured against content the question no longer holds;
    an unknown one names a key the owner has no question for. Neither is stored.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    recorded: int = Field(default=0, ge=0)
    stale: int = Field(default=0, ge=0)
    unknown: int = Field(default=0, ge=0)
