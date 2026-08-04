"""Deterministic scripted model provider used by Milestone 1 and evaluations."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from agent_core.domain.errors import ModelScriptExhaustedError
from agent_core.domain.messages import (
    FakeModelScript,
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelFailedEvent,
    ModelPermanentError,
    ModelRequest,
    ModelTurn,
    ModelUsage,
    ProviderMetadata,
    ReasoningDeltaEvent,
    ResolvedModel,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    TextDeltaEvent,
    ToolCallDeltaEvent,
    ToolCallItem,
)
from agent_core.model.streaming import ModelStreamAccumulator
from agent_core.ports.determinism import Clock


class FakeModelProvider:
    """Replay authored turns without a network, credential, or provider SDK."""

    name = "fake"

    def __init__(self, script: FakeModelScript, clock: Clock) -> None:
        self._script = script
        self._clock = clock
        self._index = 0
        self._closed = False
        self.attempts: list[ModelAttempt] = []
        self.requests: list[ModelRequest] = []

    async def stream(
        self,
        request: ModelRequest,
        resolved: ResolvedModel,
        attempt: ModelAttempt,
    ) -> AsyncIterator[ModelEvent]:
        if self._closed:
            raise RuntimeError("fake provider is closed")
        self.attempts.append(attempt.model_copy(deep=True))
        self.requests.append(request.model_copy(deep=True))
        try:
            turn = self._next_turn(request)
        except ModelScriptExhaustedError:
            yield ModelFailedEvent(
                attempt_id=attempt.attempt_id,
                run_id=attempt.run_id,
                step_number=attempt.step_number,
                sequence=0,
                error=ModelPermanentError(
                    provider=self.name,
                    model=resolved.model,
                    attempt_id=attempt.attempt_id,
                    message="the deterministic model script was exhausted",
                ),
            )
            return
        if turn.delay_ms:
            await self._clock.sleep(turn.delay_ms / 1000)
        if turn.fail_with is not None:
            error = turn.fail_with.model_copy(
                update={
                    "provider": self.name,
                    "model": resolved.model,
                    "attempt_id": attempt.attempt_id,
                },
                deep=True,
            )
            yield ModelFailedEvent(
                attempt_id=attempt.attempt_id,
                run_id=attempt.run_id,
                step_number=attempt.step_number,
                sequence=0,
                error=error,
                partial_turn=(
                    ModelTurn(
                        usage=turn.usage,
                        stop_reason=StopReason.INCOMPLETE,
                        provider_metadata=ProviderMetadata(
                            provider_api="chat_completions",
                            resolved_model=resolved.model,
                        ),
                    )
                    if turn.usage is not None
                    else None
                ),
            )
            return

        sequence = 0
        item_index = 0
        accumulator = ModelStreamAccumulator()
        if turn.reasoning:
            reasoning_event = ReasoningDeltaEvent(
                attempt_id=attempt.attempt_id,
                run_id=attempt.run_id,
                step_number=attempt.step_number,
                sequence=sequence,
                item_index=item_index,
                text=turn.reasoning,
                is_summary=False,
            )
            yield reasoning_event
            sequence += 1
            item_index += 1
        if turn.text:
            text_event = TextDeltaEvent(
                attempt_id=attempt.attempt_id,
                run_id=attempt.run_id,
                step_number=attempt.step_number,
                sequence=sequence,
                item_index=item_index,
                text=turn.text,
            )
            accumulator.add(text_event)
            yield text_event
            sequence += 1
            item_index += 1
        for scripted in turn.tool_calls:
            call = self._tool_call(scripted, attempt, item_index)
            tool_event = ToolCallDeltaEvent(
                attempt_id=attempt.attempt_id,
                run_id=attempt.run_id,
                step_number=attempt.step_number,
                sequence=sequence,
                item_index=item_index,
                call_id=call.call_id,
                name=call.name,
                arguments_delta=call.raw_arguments,
            )
            accumulator.add(tool_event)
            yield tool_event
            sequence += 1
            item_index += 1
        stop_reason = StopReason.TOOL_USE if turn.tool_calls else turn.stop_reason
        usage = turn.usage or self._usage(request, turn, resolved)
        model_turn = accumulator.turn(
            usage=usage,
            stop_reason=stop_reason,
            metadata=ProviderMetadata(
                provider_api="chat_completions",
                resolved_model=resolved.model,
            ),
        )
        yield ModelCompletedEvent(
            attempt_id=attempt.attempt_id,
            run_id=attempt.run_id,
            step_number=attempt.step_number,
            sequence=sequence,
            turn=model_turn,
            stop_reason=stop_reason,
        )

    def _next_turn(self, request: ModelRequest) -> ScriptedTurn:
        rendered_request = request.model_dump_json()
        while self._index < len(self._script.turns):
            turn = self._script.turns[self._index]
            self._index += 1
            if turn.context_contains is None or turn.context_contains in rendered_request:
                return turn
        if self._script.on_exhausted == "repeat_last":
            for turn in reversed(self._script.turns):
                if turn.context_contains is None or turn.context_contains in rendered_request:
                    return turn
        raise ModelScriptExhaustedError("fake model script exhausted")

    @staticmethod
    def _tool_call(
        scripted: ScriptedToolCall, attempt: ModelAttempt, item_index: int
    ) -> ToolCallItem:
        raw = (
            scripted.arguments
            if isinstance(scripted.arguments, str)
            else json.dumps(scripted.arguments, separators=(",", ":"), sort_keys=True)
        )
        arguments: dict[str, Any] = {}
        parse_error: str | None = None
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                arguments = decoded
            else:
                parse_error = "arguments were not an object"
        except json.JSONDecodeError:
            parse_error = "arguments were not valid JSON"
        return ToolCallItem(
            call_id=scripted.call_id or f"fake-{attempt.step_number}-{item_index}",
            item_index=item_index,
            name=scripted.name,
            arguments=arguments,
            raw_arguments=raw,
            parse_error=parse_error,
        )

    @staticmethod
    def _usage(request: ModelRequest, turn: ScriptedTurn, resolved: ResolvedModel) -> ModelUsage:
        input_bytes = len(request.model_dump_json().encode("utf-8"))
        output_bytes = len(turn.text.encode("utf-8")) + sum(
            len(json.dumps(call.arguments, sort_keys=True).encode("utf-8"))
            for call in turn.tool_calls
        )
        return ModelUsage(
            input_tokens=max(1, (input_bytes + 3) // 4),
            output_tokens=max(1, (output_bytes + 3) // 4),
            reasoning_tokens=0 if not turn.reasoning else max(1, len(turn.reasoning) // 4),
            provider="fake",
            model=resolved.model,
        )

    async def close(self) -> None:
        self._closed = True
