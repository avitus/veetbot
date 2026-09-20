"""Provider-neutral typed-judgment values.

A judgment provider answers closed, typed questions about structured state. It
generates no text: a Noul is the probability that a statement holds, a Choice
is one of the offered options with its distribution, and a Score is a position
on ordered, described levels. Question identifiers and option keys are for
code and carry no meaning of their own.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from agent_core.domain.messages import ModelUsage

JUDGMENT_KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
MAXIMUM_QUESTIONS = 8
MAXIMUM_CHOICE_OPTIONS = 16
MAXIMUM_SCORE_LEVELS = 8
MAXIMUM_TEXT_CHARACTERS = 2_000


class JudgmentFailure(StrEnum):
    AUTH_FAILED = "judgment.auth_failed"
    REQUEST_REJECTED = "judgment.request_rejected"
    RATE_LIMITED = "judgment.rate_limited"
    PROVIDER_UNAVAILABLE = "judgment.provider_unavailable"
    OUTPUT_INVALID = "judgment.output_invalid"
    INPUT_TOO_LARGE = "judgment.input_too_large"


class JudgmentProviderError(RuntimeError):
    """The one failure a judgment provider raises; its message is the code alone."""

    def __init__(self, reason: JudgmentFailure, *, retryable: bool) -> None:
        super().__init__(reason.value)
        self.reason = reason
        self.reason_code = reason.value
        self.retryable = retryable


class _JudgmentValue(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _required_text(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("text must contain non-whitespace characters")
    return normalized


class NoulQuestion(_JudgmentValue):
    """Whether a statement holds; answered with the probability of yes."""

    kind: Literal["noul"] = "noul"
    instructions: str = Field(min_length=1, max_length=MAXIMUM_TEXT_CHARACTERS)
    true_when: str | None = Field(default=None, min_length=1, max_length=MAXIMUM_TEXT_CHARACTERS)
    false_when: str | None = Field(default=None, min_length=1, max_length=MAXIMUM_TEXT_CHARACTERS)

    @field_validator("instructions")
    @classmethod
    def instructions_are_text(cls, value: str) -> str:
        return _required_text(value)

    @model_validator(mode="after")
    def criteria_come_as_a_pair(self) -> NoulQuestion:
        if (self.true_when is None) != (self.false_when is None):
            raise ValueError("noul criteria need both sides or neither")
        return self


class ChoiceOption(_JudgmentValue):
    key: str
    description: str = Field(min_length=1, max_length=MAXIMUM_TEXT_CHARACTERS)
    examples: tuple[str, ...] = Field(default=(), max_length=8)

    @field_validator("key")
    @classmethod
    def key_is_opaque(cls, value: str) -> str:
        if JUDGMENT_KEY.fullmatch(value) is None:
            raise ValueError("option key must match the judgment key grammar")
        return value

    @field_validator("description")
    @classmethod
    def description_is_text(cls, value: str) -> str:
        return _required_text(value)


class ChoiceQuestion(_JudgmentValue):
    """One of the offered options; a caller that may have no match offers one for it."""

    kind: Literal["choice"] = "choice"
    instructions: str = Field(min_length=1, max_length=MAXIMUM_TEXT_CHARACTERS)
    options: tuple[ChoiceOption, ...] = Field(min_length=2, max_length=MAXIMUM_CHOICE_OPTIONS)

    @field_validator("instructions")
    @classmethod
    def instructions_are_text(cls, value: str) -> str:
        return _required_text(value)

    @model_validator(mode="after")
    def option_keys_are_unique(self) -> ChoiceQuestion:
        keys = [option.key for option in self.options]
        if len(set(keys)) != len(keys):
            raise ValueError("choice options must have unique keys")
        return self


class ScoreQuestion(_JudgmentValue):
    """A position on ordered levels, from the first description to the last."""

    kind: Literal["score"] = "score"
    instructions: str = Field(min_length=1, max_length=MAXIMUM_TEXT_CHARACTERS)
    levels: tuple[str, ...] = Field(min_length=2, max_length=MAXIMUM_SCORE_LEVELS)

    @field_validator("instructions")
    @classmethod
    def instructions_are_text(cls, value: str) -> str:
        return _required_text(value)

    @field_validator("levels")
    @classmethod
    def levels_are_text(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_required_text(value) for value in values)


JudgmentQuestion = Annotated[
    NoulQuestion | ChoiceQuestion | ScoreQuestion, Field(discriminator="kind")
]


class JudgmentRequest(_JudgmentValue):
    """Questions in one request are answered independently over the same state."""

    state: JsonValue
    questions: dict[str, JudgmentQuestion] = Field(min_length=1, max_length=MAXIMUM_QUESTIONS)

    @field_validator("questions")
    @classmethod
    def question_keys_are_opaque(
        cls, value: dict[str, JudgmentQuestion]
    ) -> dict[str, JudgmentQuestion]:
        for key in value:
            if JUDGMENT_KEY.fullmatch(key) is None:
                raise ValueError("question key must match the judgment key grammar")
        return value


class NoulAnswer(_JudgmentValue):
    kind: Literal["noul"] = "noul"
    probability: float = Field(ge=0, le=1)


class ChoiceAnswer(_JudgmentValue):
    kind: Literal["choice"] = "choice"
    choice: str
    probabilities: dict[str, float]
    confidence: float = Field(ge=0, le=1)

    @field_validator("probabilities")
    @classmethod
    def probabilities_are_in_range(cls, value: dict[str, float]) -> dict[str, float]:
        if any(not 0 <= probability <= 1 for probability in value.values()):
            raise ValueError("choice probabilities must be between 0 and 1")
        return value


class ScoreAnswer(_JudgmentValue):
    """`score` runs from 0, the first level, to the index of the last level."""

    kind: Literal["score"] = "score"
    score: float = Field(ge=0)
    probabilities: tuple[float, ...] = Field(min_length=2, max_length=MAXIMUM_SCORE_LEVELS)
    confidence: float = Field(ge=0, le=1)

    @field_validator("probabilities")
    @classmethod
    def probabilities_are_in_range(cls, value: tuple[float, ...]) -> tuple[float, ...]:
        if any(not 0 <= probability <= 1 for probability in value):
            raise ValueError("score probabilities must be between 0 and 1")
        return value


JudgmentAnswer = Annotated[NoulAnswer | ChoiceAnswer | ScoreAnswer, Field(discriminator="kind")]


class JudgmentResult(_JudgmentValue):
    answers: dict[str, JudgmentAnswer]
    usage: ModelUsage


def validate_result(request: JudgmentRequest, result: JudgmentResult) -> None:
    """Refuse a result that does not answer exactly what the request asked."""

    invalid = JudgmentProviderError(JudgmentFailure.OUTPUT_INVALID, retryable=False)
    if set(result.answers) != set(request.questions):
        raise invalid
    for key, question in request.questions.items():
        answer = result.answers[key]
        if answer.kind != question.kind:
            raise invalid
        if isinstance(question, ChoiceQuestion) and isinstance(answer, ChoiceAnswer):
            offered = {option.key for option in question.options}
            if answer.choice not in offered or set(answer.probabilities) != offered:
                raise invalid
        if isinstance(question, ScoreQuestion) and isinstance(answer, ScoreAnswer):
            if len(answer.probabilities) != len(question.levels):
                raise invalid
            if answer.score > len(question.levels) - 1:
                raise invalid
