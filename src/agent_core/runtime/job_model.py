"""Explicit, tool-free job completions charged to the executing run."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncGenerator
from contextlib import aclosing
from decimal import Decimal
from typing import Any, cast

from agent_core.domain.errors import BudgetExceededError
from agent_core.domain.jobs import JobBrokerError
from agent_core.domain.messages import (
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelFailedEvent,
    ModelRequest,
    ModelUsage,
    ResolvedModel,
    TextPart,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import BudgetScope, Run, Step
from agent_core.model.cost import MILLION, highest_input_rate
from agent_core.model.streaming import validated_stream
from agent_core.ports.determinism import Clock, IdFactory
from agent_core.ports.models import ModelProvider
from agent_core.ports.repositories import BudgetLedger


class JobModelCompletion:
    def __init__(
        self,
        *,
        run: Run,
        provider: ModelProvider,
        resolved: ResolvedModel,
        budgets: BudgetLedger,
        clock: Clock,
        ids: IdFactory,
    ) -> None:
        self.run = run
        self.provider = provider
        self.resolved = resolved
        self.budgets = budgets
        self.clock = clock
        self.ids = ids

    async def complete(
        self, *, prompt: str, max_output_tokens: int, maximum_cost: Decimal, call_id: str
    ) -> dict[str, Any]:
        """Make one tool-free completion within grant and run budgets and persist its usage."""
        # One token per UTF-8 byte plus framing is a conservative upper bound
        # for this plain text request. No tools, images or cache writes join it.
        input_bound = len(prompt.encode()) + 512
        output_bound = min(max_output_tokens, self.resolved.limits.max_output_tokens)
        pricing = self.resolved.pricing
        reserve = (
            input_bound * highest_input_rate(pricing)
            + output_bound * max(pricing.output_per_mtok, pricing.reasoning_per_mtok or Decimal(0))
        ) / MILLION
        run_remaining = (
            maximum_cost
            if self.run.limits.max_cost is None
            else self.run.limits.max_cost - self.run.usage.cost
        )
        if maximum_cost <= 0 or reserve > min(maximum_cost, run_remaining):
            raise JobBrokerError("job.model_budget_exceeded")
        if input_bound + output_bound > self.resolved.limits.context_window_tokens:
            raise JobBrokerError("job.model_context_limit")
        try:
            self.budgets.check(self.run, BudgetScope.ATTEMPT)
        except BudgetExceededError as exc:
            if exc.reason == "budget_exceeded":
                raise JobBrokerError("job.model_budget_exceeded") from None
            raise
        request = ModelRequest(
            model_policy=self.resolved.policy_name,
            conversation=[
                UserMessage(content=[TextPart(text=prompt)], trust=TrustLevel.EXTERNAL_UNTRUSTED)
            ],
            tools=[],
            tool_choice="none",
            maximum_output_tokens=output_bound,
            maximum_provider_attempts=1,
            timeout_seconds=60,
            metadata={
                "prefix_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                "job_call": call_id,
            },
        )
        step = Step(
            run_id=self.run.id, step_number=max(1, self.run.step_count), started_at=self.clock.now()
        )
        attempt = ModelAttempt(
            attempt_id=self.ids.new_id(),
            run_id=self.run.id,
            step_number=step.step_number,
            attempt_number=1,
            started_at=self.clock.now(),
        )
        terminal: ModelCompletedEvent | ModelFailedEvent | None = None
        stream = cast(
            AsyncGenerator[ModelEvent, None], self.provider.stream(request, self.resolved, attempt)
        )
        async with aclosing(stream):
            async for event in validated_stream(stream):
                if isinstance(event, (ModelCompletedEvent, ModelFailedEvent)):
                    terminal = event
        if terminal is None:
            raise JobBrokerError("job.model_failed")
        turn = terminal.turn if isinstance(terminal, ModelCompletedEvent) else terminal.partial_turn
        usage = (
            turn.usage
            if turn is not None
            else ModelUsage(provider=self.resolved.provider, model=self.resolved.model)
        )
        await self.budgets.record_model_usage(
            self.run,
            usage,
            step=step,
            attempt=attempt,
            request=request,
            resolved_model=self.resolved,
            model_turn=turn,
            registry_version=None
            if self.run.provider_pin is None
            else self.run.provider_pin.registry_version,
            stop_reason=None if turn is None else turn.stop_reason,
            error_kind="permanent" if isinstance(terminal, ModelFailedEvent) else None,
        )
        if isinstance(terminal, ModelFailedEvent) or turn is None or turn.tool_calls:
            raise JobBrokerError("job.model_failed")
        if usage.cost > maximum_cost:
            raise JobBrokerError("job.model_budget_exceeded")
        text = "\n".join(
            part.text
            for message in turn.assistant_messages
            for part in message.content
            if isinstance(part, TextPart)
        )
        if len(text.encode()) > 48 * 1024:
            raise JobBrokerError("job.response_limit")
        return {"text": text, "cost": str(usage.cost)}
