"""Provider-neutral typed-judgment port."""

from __future__ import annotations

from typing import Protocol

from agent_core.domain.judgment import JudgmentRequest, JudgmentResult


class JudgmentProvider(Protocol):
    name: str

    async def judge(self, request: JudgmentRequest) -> JudgmentResult: ...

    async def close(self) -> None: ...
