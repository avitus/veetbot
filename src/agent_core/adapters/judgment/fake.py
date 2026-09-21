"""Scripted judgment provider for contract, unit, and composed tests."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from agent_core.domain.judgment import (
    JudgmentProviderError,
    JudgmentRequest,
    JudgmentResult,
    validate_result,
)

JudgmentScriptStep = JudgmentResult | JudgmentProviderError
JudgmentScript = Sequence[JudgmentScriptStep] | Callable[[JudgmentRequest], JudgmentScriptStep]


class FakeJudgmentProvider:
    """Replay scripted results in order, or compute one per request, recording every request."""

    name = "fake"

    def __init__(self, script: JudgmentScript) -> None:
        self._script = script
        self._position = 0
        self.requests: list[JudgmentRequest] = []
        self.closed = False

    async def judge(self, request: JudgmentRequest) -> JudgmentResult:
        self.requests.append(request)
        if callable(self._script):
            step = self._script(request)
        else:
            if self._position >= len(self._script):
                raise AssertionError("fake judgment provider script is exhausted")
            step = self._script[self._position]
            self._position += 1
        if isinstance(step, JudgmentProviderError):
            raise step
        validate_result(request, step)
        return step

    async def close(self) -> None:
        self.closed = True
