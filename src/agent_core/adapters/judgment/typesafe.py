"""TypeSafe implementation of the provider-neutral typed-judgment port."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping
from decimal import Decimal

import httpx
from pydantic import ValidationError

from agent_core.domain.credentials import CredentialRef
from agent_core.domain.judgment import (
    ChoiceAnswer,
    ChoiceOption,
    ChoiceQuestion,
    JudgmentAnswer,
    JudgmentFailure,
    JudgmentProviderError,
    JudgmentRequest,
    JudgmentResult,
    NoulAnswer,
    NoulQuestion,
    ScoreAnswer,
    ScoreQuestion,
    validate_result,
)
from agent_core.domain.messages import CostSource, ModelUsage
from agent_core.ports.credentials import CredentialResolver
from agent_core.ports.determinism import Clock

# The one destination this adapter dials. No setting, request field, or response
# changes it, and the client follows no redirect.
TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
TYPESAFE_MODEL_ALIAS = "jev-latest"
MAXIMUM_REQUEST_BYTES = 64 * 1024
MAXIMUM_RESPONSE_BYTES = 256 * 1024
MAXIMUM_ATTEMPTS = 3
# Fixed delays taken from the injected clock; ambient randomness is unavailable here.
RETRY_DELAYS_SECONDS = (0.25, 0.5)
# USD per million input tokens as published on 2026-09-19; output tokens are free.
INPUT_PRICE_PER_MILLION_TOKENS = Decimal("0.042")

_CREDENTIAL = CredentialRef("typesafe")
_MODEL_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_AUTH_FAILURE_STATUSES = frozenset({401, 403})
_UNAVAILABLE_STATUSES = frozenset({408, 425})


def _failure(reason: JudgmentFailure, *, retryable: bool = False) -> JudgmentProviderError:
    return JudgmentProviderError(reason, retryable=retryable)


def _wire_option(option: ChoiceOption) -> object:
    if not option.examples:
        return option.description
    return {"what": option.description, "examples": list(option.examples)}


def _wire_question(question: NoulQuestion | ChoiceQuestion | ScoreQuestion) -> dict[str, object]:
    wire: dict[str, object] = {"type": question.kind, "instructions": question.instructions}
    if isinstance(question, NoulQuestion):
        if question.true_when is not None and question.false_when is not None:
            wire["criteria"] = {"true": question.true_when, "false": question.false_when}
    elif isinstance(question, ChoiceQuestion):
        wire["criteria"] = {option.key: _wire_option(option) for option in question.options}
    else:
        wire["criteria"] = list(question.levels)
    return wire


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a number")
    return float(value)


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("expected a non-negative integer")
    return value


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("expected an object")
    return value


def _answer(
    question: NoulQuestion | ChoiceQuestion | ScoreQuestion, wire: Mapping[str, object]
) -> JudgmentAnswer:
    if wire.get("type") != question.kind:
        raise ValueError("answer kind does not match its question")
    if isinstance(question, NoulQuestion):
        return NoulAnswer(probability=_number(wire["noul"]))
    probabilities = _mapping(wire["probabilities"])
    if isinstance(question, ChoiceQuestion):
        choice = wire["choice"]
        if not isinstance(choice, str):
            raise ValueError("expected a chosen option key")
        return ChoiceAnswer(
            choice=choice,
            probabilities={str(key): _number(value) for key, value in probabilities.items()},
            confidence=_number(wire["confidence"]),
        )
    # The vendor keys score levels by their index, "0" through the last level.
    if set(probabilities) != {str(index) for index in range(len(question.levels))}:
        raise ValueError("score probabilities do not match the offered levels")
    return ScoreAnswer(
        score=_number(wire["score"]),
        probabilities=tuple(
            _number(probabilities[str(index)]) for index in range(len(question.levels))
        ),
        confidence=_number(wire["confidence"]),
    )


class TypeSafeJudgmentProvider:
    name = "typesafe"

    def __init__(
        self,
        *,
        credentials: CredentialResolver,
        clock: Clock,
        client: httpx.AsyncClient | None = None,
        attempt_timeout_seconds: float = 4.0,
        total_timeout_seconds: float = 10.0,
    ) -> None:
        self._credentials = credentials
        self._clock = clock
        self._client = client or httpx.AsyncClient(follow_redirects=False)
        self._owns_client = client is None
        self._attempt_timeout_seconds = attempt_timeout_seconds
        self._total_timeout_seconds = total_timeout_seconds

    async def judge(self, request: JudgmentRequest) -> JudgmentResult:
        body = json.dumps(
            {
                "state": request.state,
                "model": TYPESAFE_MODEL_ALIAS,
                "questions": {
                    key: _wire_question(question) for key, question in request.questions.items()
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(body) > MAXIMUM_REQUEST_BYTES:
            raise _failure(JudgmentFailure.INPUT_TOO_LARGE) from None
        try:
            async with asyncio.timeout(self._total_timeout_seconds):
                received = await self._send(body)
        except TimeoutError:
            raise _failure(JudgmentFailure.PROVIDER_UNAVAILABLE, retryable=True) from None
        result = self._result(request, received)
        validate_result(request, result)
        return result

    async def _send(self, body: bytes) -> bytes:
        for attempt in range(1, MAXIMUM_ATTEMPTS + 1):
            try:
                return await self._attempt(body)
            except JudgmentProviderError as failure:
                if not failure.retryable or attempt == MAXIMUM_ATTEMPTS:
                    raise
            await self._clock.sleep(RETRY_DELAYS_SECONDS[attempt - 1])
        raise AssertionError("judgment retry loop exited without a result")

    async def _attempt(self, body: bytes) -> bytes:
        # Resolved per attempt and never stored, so the key lives on no object.
        try:
            secret = await self._credentials.resolve(_CREDENTIAL)
        except PermissionError:
            raise _failure(JudgmentFailure.AUTH_FAILED) from None
        received = bytearray()
        try:
            async with self._client.stream(
                "POST",
                TYPESAFE_ENDPOINT,
                headers={
                    "Authorization": "Bearer " + secret.reveal(),
                    "Content-Type": "application/json",
                },
                content=body,
                timeout=self._attempt_timeout_seconds,
            ) as response:
                # A failed response is classified by status alone; its body is never read.
                status = response.status_code
                if status in _AUTH_FAILURE_STATUSES:
                    raise _failure(JudgmentFailure.AUTH_FAILED) from None
                if status == 429:
                    raise _failure(JudgmentFailure.RATE_LIMITED, retryable=True) from None
                if status in _UNAVAILABLE_STATUSES or status >= 500:
                    raise _failure(JudgmentFailure.PROVIDER_UNAVAILABLE, retryable=True) from None
                if status < 200 or status >= 300:
                    raise _failure(JudgmentFailure.REQUEST_REJECTED) from None
                async for chunk in response.aiter_bytes():
                    received.extend(chunk)
                    if len(received) > MAXIMUM_RESPONSE_BYTES:
                        raise _failure(JudgmentFailure.OUTPUT_INVALID) from None
        except JudgmentProviderError:
            raise
        except (httpx.TimeoutException, httpx.TransportError):
            raise _failure(JudgmentFailure.PROVIDER_UNAVAILABLE, retryable=True) from None
        return bytes(received)

    @staticmethod
    def _result(request: JudgmentRequest, received: bytes) -> JudgmentResult:
        # Validation errors quote their input, so nothing here may chain its cause.
        try:
            decoded = _mapping(json.loads(received))
            answers = _mapping(decoded["answers"])
            if set(answers) != set(request.questions):
                raise ValueError("answers do not match the questions asked")
            usage = _mapping(decoded["usage"])
            input_tokens = _count(usage["input_tokens"])
            model = decoded.get("model")
            return JudgmentResult(
                answers={
                    key: _answer(question, _mapping(answers[key]))
                    for key, question in request.questions.items()
                },
                usage=ModelUsage(
                    input_tokens=input_tokens,
                    output_tokens=_count(usage["output_tokens"]),
                    cost=INPUT_PRICE_PER_MILLION_TOKENS * input_tokens / 1_000_000,
                    cost_source=CostSource.DOCS_SNAPSHOT,
                    provider=TypeSafeJudgmentProvider.name,
                    # The name reaches audits, so an unexpected shape yields the alias.
                    model=(
                        model
                        if isinstance(model, str) and _MODEL_NAME.fullmatch(model)
                        else TYPESAFE_MODEL_ALIAS
                    ),
                ),
            )
        except (KeyError, TypeError, ValueError, ValidationError):
            raise _failure(JudgmentFailure.OUTPUT_INVALID) from None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
