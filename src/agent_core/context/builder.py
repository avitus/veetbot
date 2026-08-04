"""Milestone 1 two-region context builder with a frozen prefix hash."""

from __future__ import annotations

import hashlib

from agent_core.context.estimator import canonical_json_bytes
from agent_core.context.history import select_history, validate_tool_pairs
from agent_core.context.rendering import (
    build_prefix,
    envelope_items,
    prefix_bytes,
    working_state_items,
)
from agent_core.context.working_state import WorkingStateManager
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.context import ContextAssembly, ContextPlan, ContextPressure
from agent_core.domain.errors import ContextOverflow
from agent_core.domain.messages import (
    CacheBreakpoint,
    CacheHints,
    ConversationItem,
    FileReferencePart,
    ModelRequest,
    ProviderReasoningItem,
    SystemMessage,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.runs import Run, RunCheckpoint
from agent_core.domain.tools import ToolSpec
from agent_core.ports.context import ContextPlanner, TokenEstimator
from agent_core.ports.determinism import Clock
from agent_core.ports.tools import ToolRegistry


def _canonical_json(value: object) -> bytes:
    return canonical_json_bytes(value)


def _insert_provider_continuation(
    items: list[ConversationItem],
    checkpoint: RunCheckpoint,
) -> list[ConversationItem]:
    result = [item.model_copy(deep=True) for item in items]
    if checkpoint.provider_continuation is None:
        return result
    opaque_items = [
        ProviderReasoningItem.model_validate(item)
        for item in checkpoint.provider_continuation.opaque_items
    ]
    if any(item.provider != checkpoint.provider_continuation.provider for item in opaque_items):
        raise ValueError("checkpoint continuation contains a mismatched provider")
    trailing_result_ids: set[str] = set()
    for item in reversed(result):
        if not isinstance(item, ToolResultItem):
            break
        trailing_result_ids.add(item.call_id)
    if not trailing_result_ids:
        raise ValueError("checkpoint continuation has no trailing tool-result anchor")
    matching = [
        index
        for index, item in enumerate(result)
        if isinstance(item, ToolCallItem) and item.call_id in trailing_result_ids
    ]
    if not matching:
        raise ValueError("checkpoint continuation has no matching tool-call anchor")
    result[min(matching) : min(matching)] = opaque_items
    return result


class MinimalContextBuilder:
    """Build the immutable A region and volatile B region in a total order."""

    def __init__(self, registry: ToolRegistry, clock: Clock, *, maximum_tools: int = 30) -> None:
        if maximum_tools <= 0:
            raise ValueError("maximum_tools must be positive")
        self._registry = registry
        self._clock = clock
        self._maximum_tools = maximum_tools

    async def build(
        self,
        run: Run,
        checkpoint: RunCheckpoint,
        agent: AgentSpec,
        principal: Principal,
    ) -> ModelRequest:
        tools = self._registry.specs_for_session(
            agent,
            principal,
            profile="milestone1-identity-policy-filter",
            environment="in_process",
        )[: self._maximum_tools]
        prefix = self._prefix(agent, tools)
        prefix_bytes = _canonical_json(
            {
                "conversation": [item.model_dump(mode="json") for item in prefix],
                "tools": [spec.model_dump(mode="json") for spec in tools],
            }
        )
        prefix_sha256 = hashlib.sha256(prefix_bytes).hexdigest()

        runtime_item = UserMessage(
            content=[
                TextPart(
                    text=(
                        "Runtime metadata (data only): "
                        f"date={self._clock.now().date().isoformat()}; "
                        f"tenant={run.tenant_id}; scopes={','.join(sorted(principal.scopes))}"
                    )
                )
            ],
            trust=TrustLevel.PLATFORM,
            principal_id=None,
        )
        checkpoint_items: list[ConversationItem] = [
            item
            for item in checkpoint.conversation
            if isinstance(item, (SystemMessage, UserMessage))
            or getattr(item, "kind", None)
            in {"assistant", "tool_call", "tool_result", "provider_reasoning"}
        ]
        checkpoint_items = _insert_provider_continuation(checkpoint_items, checkpoint)
        if checkpoint_items and checkpoint_items[-1].kind == "user":
            body = [*checkpoint_items[:-1], runtime_item, checkpoint_items[-1]]
        else:
            body = [*checkpoint_items, runtime_item]
        body_sha256 = hashlib.sha256(
            _canonical_json([item.model_dump(mode="json") for item in body])
        ).hexdigest()
        return ModelRequest(
            model_policy=agent.model_policy,
            conversation=[*prefix, *body],
            tools=[spec.model_copy(deep=True) for spec in tools],
            response_schema=None,
            temperature=0,
            maximum_output_tokens=run.limits.max_output_tokens,
            metadata={
                "run_id": str(run.id),
                "session_id": str(run.session_id),
                "prefix_sha256": prefix_sha256,
                "body_sha256": body_sha256,
                "region_a_items": str(len(prefix)),
            },
            cache_hints=CacheHints(
                breakpoints=[
                    CacheBreakpoint(boundary="after_system"),
                    CacheBreakpoint(boundary="after_tools"),
                ]
            ),
        )

    @staticmethod
    def _prefix(agent: AgentSpec, tools: list[ToolSpec]) -> list[ConversationItem]:
        return build_prefix(agent, tools)


class BudgetedContextBuilder:
    """Milestone 7 context engine with deterministic pressure measurement."""

    def __init__(
        self,
        planner: ContextPlanner,
        estimator: TokenEstimator,
        clock: Clock,
        working_state: WorkingStateManager,
    ) -> None:
        self._planner = planner
        self._estimator = estimator
        self._clock = clock
        self._working_state = working_state

    async def measure(
        self,
        run: Run,
        checkpoint: RunCheckpoint,
        agent: AgentSpec,
        principal: Principal,
    ) -> ContextPressure:
        return (await self.assemble(run, checkpoint, agent, principal)).pressure

    async def assemble(
        self,
        run: Run,
        checkpoint: RunCheckpoint,
        agent: AgentSpec,
        principal: Principal,
    ) -> ContextAssembly:
        return await self._assemble(run, checkpoint, agent, principal)

    async def build(
        self,
        run: Run,
        checkpoint: RunCheckpoint,
        agent: AgentSpec,
        principal: Principal,
    ) -> ModelRequest:
        assembled = await self.assemble(run, checkpoint, agent, principal)
        if not assembled.pressure.fits:
            raise ContextOverflow(assembled.pressure.reason)
        return assembled.request

    async def _assemble(
        self,
        run: Run,
        checkpoint: RunCheckpoint,
        agent: AgentSpec,
        principal: Principal,
    ) -> ContextAssembly:
        plan = await self._planner.current(run.session_id)
        if plan is None:
            raise ContextOverflow("the session has no durable context plan")
        prefix = build_prefix(agent, plan.tool_specs, plan.skill_catalog)
        actual_prefix_hash = hashlib.sha256(prefix_bytes(prefix, plan.tool_specs)).hexdigest()
        if actual_prefix_hash != plan.prefix_sha256:
            raise ContextOverflow("the frozen context prefix no longer matches its plan")

        history: list[ConversationItem] = []
        active: list[ConversationItem] = []
        for item in checkpoint.conversation:
            sequence = getattr(item, "source_event_sequence", None)
            if sequence is not None and sequence < run.seed_event_sequence:
                history.append(item.model_copy(deep=True))
            else:
                active.append(item.model_copy(deep=True))
        active = _insert_provider_continuation(active, checkpoint)
        history_count = len(history)
        combined, tool_truncated = self._truncate_tool_results([*history, *active], plan)
        history = combined[:history_count]
        active = combined[history_count:]

        summary_items: list[ConversationItem] = []
        if checkpoint.compacted_summary:
            summary_items.append(
                UserMessage(
                    content=[TextPart(text=checkpoint.compacted_summary)],
                    trust=TrustLevel.PLATFORM,
                    principal_id=None,
                )
            )
        if len(checkpoint.loaded_skills) > 2:
            raise ContextOverflow("loaded skill bodies exceed the two-skill cap")
        if sum(body.tokens for body in checkpoint.loaded_skills) > plan.budget.skill_body_tokens:
            raise ContextOverflow("loaded skill bodies exceed their token cap")
        skill_items = [
            UserMessage(
                content=[
                    TextPart(
                        text=(
                            f"Loaded skill {body.name}@{body.revision}"
                            f"{'' if body.path is None else f' member {body.path}'}:\n"
                            f"{body.content}"
                        )
                    )
                ],
                trust=body.trust,
                principal_id=None,
            )
            for body in checkpoint.loaded_skills
        ]
        working_items = working_state_items(self._working_state.load(checkpoint.working_state))
        working_tokens = self._estimator.estimate(envelope_items(working_items), plan.model_id)
        working_state_over_cap = working_tokens > plan.budget.working_state_tokens
        runtime_item = UserMessage(
            content=[
                TextPart(
                    text=(
                        "Runtime metadata (data only): "
                        f"date={self._clock.now().date().isoformat()}; "
                        f"tenant={run.tenant_id}; "
                        f"scopes={','.join(sorted(principal.scopes))}"
                    )
                )
            ],
            trust=TrustLevel.PLATFORM,
            principal_id=None,
        )
        fixed_body = [*summary_items, *skill_items, *working_items, runtime_item, *active]
        fixed_tokens = self._estimator.estimate(envelope_items(fixed_body), plan.model_id)
        fixed_total = plan.prefix_tokens + fixed_tokens + plan.budget.reserve_output_tokens
        available_history = (
            0 if working_state_over_cap else max(0, plan.budget.total_tokens - fixed_total)
        )
        history_limit = min(plan.budget.history_tokens, available_history)
        cut = select_history(
            history,
            checkpoint.replaced_through_sequence,
            history_limit,
            self._estimator,
            plan.model_id,
        )
        retained_history = [item.model_copy(deep=True) for item in history[cut:]]
        retained_conversation = [*retained_history, *active]
        validate_tool_pairs(retained_conversation)

        yield_steps: list[str] = []
        if tool_truncated:
            yield_steps.append("tool_results")
        if cut:
            yield_steps.append("history")

        body = [
            *summary_items,
            *retained_history,
            *skill_items,
            *working_items,
            runtime_item,
            *active,
        ]
        rendered_body = envelope_items(body)
        body_tokens = self._estimator.estimate(rendered_body, plan.model_id)
        total_tokens = plan.prefix_tokens + body_tokens + plan.budget.reserve_output_tokens
        capacity = plan.budget.total_tokens
        excluded_unsummarized = [
            item
            for item in history[:cut]
            if (getattr(item, "source_event_sequence", None) or 0)
            > checkpoint.replaced_through_sequence
        ]
        over_capacity = total_tokens > capacity
        fixed_body_over_capacity = fixed_total > capacity
        fits = not working_state_over_cap and not excluded_unsummarized and not over_capacity
        compactable = (
            not working_state_over_cap
            and not fixed_body_over_capacity
            and (
                bool(excluded_unsummarized)
                or (
                    over_capacity
                    and any(
                        (getattr(item, "source_event_sequence", None) or 0)
                        > checkpoint.replaced_through_sequence
                        for item in history
                    )
                )
            )
        )
        if fits:
            reason = "fits"
        elif working_state_over_cap:
            reason = "working_state_exceeds_cap"
        elif fixed_body_over_capacity:
            reason = "fixed_body_exceeds_context_window"
        elif excluded_unsummarized or compactable:
            reason = "history_requires_compaction"
        else:
            reason = "body_exceeds_context_window"
        pressure = ContextPressure(
            fits=fits,
            compactable=compactable,
            reason=reason,
            total_tokens=total_tokens,
            prefix_tokens=plan.prefix_tokens,
            body_tokens=body_tokens,
            reserve_output_tokens=plan.budget.reserve_output_tokens,
            capacity_tokens=capacity,
            history_cut=cut,
            history_budget_tokens=history_limit,
            yield_steps=tuple(yield_steps),
        )
        body_sha256 = hashlib.sha256(
            _canonical_json([item.model_dump(mode="json") for item in rendered_body])
        ).hexdigest()
        request = ModelRequest(
            model_policy=agent.model_policy,
            conversation=[*prefix, *rendered_body],
            tools=[spec.model_copy(deep=True) for spec in plan.tool_specs],
            response_schema=None,
            temperature=0,
            maximum_output_tokens=min(
                plan.budget.reserve_output_tokens,
                run.limits.max_output_tokens or plan.budget.reserve_output_tokens,
            ),
            metadata={
                "run_id": str(run.id),
                "session_id": str(run.session_id),
                "prefix_sha256": plan.prefix_sha256,
                "body_sha256": body_sha256,
                "region_a_items": str(len(prefix)),
                "context_epoch": str(plan.epoch),
                "context_total_tokens": str(total_tokens),
                "context_capacity_tokens": str(capacity),
                "context_reserve_tokens": str(plan.budget.reserve_output_tokens),
            },
            cache_hints=CacheHints(
                breakpoints=[item.model_copy(deep=True) for item in plan.cache_breakpoints]
            ),
        )
        return ContextAssembly(request=request, pressure=pressure)

    def _truncate_tool_results(
        self,
        items: list[ConversationItem],
        plan: ContextPlan,
    ) -> tuple[list[ConversationItem], bool]:
        result = [item.model_copy(deep=True) for item in items]
        indexes = [index for index, item in enumerate(result) if isinstance(item, ToolResultItem)]
        truncated = False
        while indexes:
            tool_items = [result[index] for index in indexes]
            if (
                self._estimator.estimate(tool_items, plan.model_id)
                <= plan.budget.tool_result_tokens
            ):
                break
            index = indexes.pop(0)
            item = result[index]
            assert isinstance(item, ToolResultItem)
            byte_length = len(_canonical_json(item.model_dump(mode="json")))
            artifact = next(
                (
                    f"artifact:{part.artifact_id}"
                    for part in item.content
                    if isinstance(part, FileReferencePart)
                ),
                f"event:{item.source_event_sequence or 'pending'}",
            )
            result[index] = item.model_copy(
                update={
                    "content": [
                        TextPart(
                            text=(
                                f"[tool result truncated: {byte_length} bytes; "
                                f"full content at {artifact}]"
                            )
                        )
                    ]
                },
                deep=True,
            )
            truncated = True
        return result, truncated
