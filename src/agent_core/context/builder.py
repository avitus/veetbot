"""Milestone 1 two-region context builder with a frozen prefix hash."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from uuid import UUID
from zoneinfo import ZoneInfo

from agent_core.context.estimator import canonical_json_bytes
from agent_core.context.history import select_history, validate_tool_pairs
from agent_core.context.recall_budget import (
    FULL_ALLOWANCE,
    RecallAllowance,
    context_ranking,
    ranking_block,
    select_current_search_results,
)
from agent_core.context.records_catalog import (
    CATALOG_SCOPE,
    RecordsCatalogSource,
    catalog_item,
    render_records_catalog,
)
from agent_core.context.rendering import (
    build_prefix,
    envelope_items,
    prefix_bytes,
    working_state_items,
)
from agent_core.context.working_state import WorkingStateManager
from agent_core.domain.agents import AgentSpec, Principal
from agent_core.domain.browser import WEBSITE_OFFER_STATE_KEY
from agent_core.domain.cache import session_cache_key
from agent_core.domain.context import ContextAssembly, ContextPlan, ContextPressure, WorkingState
from agent_core.domain.errors import ContextOverflow
from agent_core.domain.memory import (
    MemoryCorrection,
    RecallProfile,
    RecallQuery,
    RecallResult,
    Sensitivity,
)
from agent_core.domain.messages import (
    CacheBreakpoint,
    CacheHints,
    ContentPart,
    ConversationItem,
    FileReferencePart,
    ModelRequest,
    ProviderReasoningItem,
    RecallRankingBlock,
    SystemMessage,
    TextPart,
    ToolCallItem,
    ToolResultItem,
    UserMessage,
)
from agent_core.domain.policies import TrustLevel
from agent_core.domain.recall_execution import EXHAUSTED, RecallBudgetExceededError
from agent_core.domain.runs import Run, RunCheckpoint
from agent_core.domain.tool_output import content_bytes, context_view, output_excerpt
from agent_core.domain.tools import ToolSpec, deferred_definition_text

# The delta block is the same rendering the retriever produced for the base
# one, taken over the items the base recall did not already carry.
from agent_core.memory.retrieval import render_memory
from agent_core.ports.context import ContextPlanner, TokenEstimator
from agent_core.ports.determinism import Clock
from agent_core.ports.memory import MemoryRetriever, QueryFormer
from agent_core.ports.tools import ToolRegistry

MAX_LOADED_SKILL_BODIES = 2
# Tool-result truncation advances in this many blocks of its budget (ADR-0242).
TOOL_RESULT_TRUNCATION_BLOCKS = 4
# ADR-0258: while results are truncated, the note names at most this many of
# the newest results still shown, by the event a working-state fact cites.
TRUNCATION_NOTE_RESULTS = 12
# The context package does not import the tools package (as in the planner).
_TOOL_CALL_TOOL_NAME = "tool.call"
_WORKING_STATE_TOOL_NAME = "context.update_working_state"
TRUNCATION_NOTE = (
    "Tool-result note (data only): older tool results in this conversation are "
    "truncated to pointers to fit the context, and no tool reads a truncated result "
    "back. Do not repeat a call only to recover one."
)
TRUNCATION_WORKING_STATE_ADVICE = (
    "Keep each finding you will still need as a fact with "
    "context.update_working_state, citing the event of the result it came from; "
    "working state is shown on every step."
)

# The shipped classes.records_catalog.max_items; composition passes the knob.
CATALOG_MAX_SERIES = 16
CATALOG_ZONE = ZoneInfo("UTC")
RECALL_CACHE_CAPACITY = 1_024
AUTOMATIC_RECALL_SECONDS = 10.0
RECALL_TIMED_OUT = (
    "Automatic memory recall exceeded its time allowance. This does not mean no memory exists."
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _RecallBundle:
    """What one turn's memory read produced, in assembly order.

    `base` and `delta` are already-rendered memory blocks and are droppable
    under budget pressure; `corrections` override the frozen snapshot and are
    not. An empty bundle is what a turn without a user message, without a
    retriever, or with a failed recall assembles.
    """

    base: str | None = None
    delta: str | None = None
    corrections: tuple[MemoryCorrection, ...] = ()
    rankings: tuple[RecallRankingBlock | None, ...] = ()


def current_user_text(items: list[ConversationItem]) -> str | None:
    for item in reversed(items):
        if isinstance(item, UserMessage) and item.trust is TrustLevel.USER:
            text = "\n".join(
                part.text for part in item.content if isinstance(part, TextPart)
            ).strip()
            return text or None
    return None


def _with_step_context(
    items: list[ConversationItem], additions: list[ConversationItem]
) -> tuple[list[ConversationItem], int]:
    """Keep completed exchanges before fresh context; a new user input stays last.

    The returned index closes the portable prefix before the changing rows.
    Never search backward for the run's original user message: after tool calls
    it belongs to completed history, and inserting there invalidates every tool
    exchange whenever recall, working state, skills or the date changes.
    """
    split = len(items)
    if items and isinstance(items[-1], UserMessage) and items[-1].trust is TrustLevel.USER:
        split -= 1
    return (
        [item.model_copy(deep=True) for item in [*items[:split], *additions, *items[split:]]],
        split,
    )


def _canonical_json(value: object) -> bytes:
    return canonical_json_bytes(value)


def _argument_refusal(part: ContentPart) -> bool:
    if not isinstance(part, TextPart):
        return False
    try:
        outcome = json.loads(part.text)
    except ValueError:
        return False
    return isinstance(outcome, dict) and outcome.get("reason_code") == "tool.arguments_invalid"


def _definition_answers(items: Sequence[ConversationItem], plan: ContextPlan) -> set[int]:
    """The newest answer that carries each deferred tool's definition (ADR-0258).

    An invalid call through ``tool.call`` answers with the platform outcome and
    the pinned definition, and nothing else. Only that exact pair is kept
    whole: a tool's own output, or the schema-only answer of the earlier
    contract, truncates as before.
    """

    definitions = {spec.name: deferred_definition_text(spec) for spec in plan.deferred_tool_specs}
    named: dict[str, str] = {}
    for item in items:
        if isinstance(item, ToolCallItem) and item.name == _TOOL_CALL_TOOL_NAME:
            name = item.arguments.get("name")
            if isinstance(name, str) and name in definitions:
                named[item.call_id] = name
    newest: dict[str, int] = {}
    for index, item in enumerate(items):
        if not isinstance(item, ToolResultItem) or not item.is_error or len(item.content) != 2:
            continue
        name = named.get(item.call_id)
        outcome, answer = item.content
        if (
            name is not None
            and isinstance(answer, TextPart)
            and answer.text == definitions[name]
            and _argument_refusal(outcome)
        ):
            newest[name] = index
    return set(newest.values())


def _tool_result_pointer(item: ToolResultItem) -> ToolResultItem:
    """The pointer that replaces a truncated result (ADR-0242, ADR-0258)."""

    byte_length = len(_canonical_json(item.model_dump(mode="json")))
    location = next(
        (
            # A fact cites its result's event, so an artifact pointer names the
            # recording event too (ADR-0258).
            f"artifact:{part.artifact_id}"
            + (
                f"; event:{item.source_event_sequence}"
                if item.source_event_sequence is not None
                else ""
            )
            for part in item.content
            if isinstance(part, FileReferencePart)
        ),
        f"event:{item.source_event_sequence or 'pending'}",
    )
    return item.model_copy(
        update={
            "content": [
                TextPart(
                    text=f"[tool result truncated: {byte_length} bytes; full content at {location}]"
                )
            ]
        },
        deep=True,
    )


def _truncation_note(
    items: Sequence[ConversationItem], truncated: set[int], plan: ContextPlan
) -> UserMessage:
    """Say that truncated results are gone, and what a fact can cite (ADR-0258).

    Only names the plan pins reach this platform row: a call's own name when it
    has a definition, the named tool when it went through ``tool.call``.
    """

    text = TRUNCATION_NOTE
    if _WORKING_STATE_TOOL_NAME in plan.tool_names:
        called: dict[str, str] = {}
        for item in items:
            if not isinstance(item, ToolCallItem):
                continue
            name = item.arguments.get("name") if item.name == _TOOL_CALL_TOOL_NAME else item.name
            if isinstance(name, str) and (
                name in plan.deferred_tool_names
                if item.name == _TOOL_CALL_TOOL_NAME
                else name in plan.tool_names
            ):
                called[item.call_id] = name
        shown = [
            f"event {item.source_event_sequence}"
            + (f" ({called[item.call_id]})" if item.call_id in called else "")
            for index, item in enumerate(items)
            if isinstance(item, ToolResultItem)
            and index not in truncated
            and item.source_event_sequence is not None
        ][-TRUNCATION_NOTE_RESULTS:]
        text = f"{text} {TRUNCATION_WORKING_STATE_ADVICE}"
        if shown:
            text = f"{text} Results still shown, newest last: {'; '.join(shown)}."
    return UserMessage(content=[TextPart(text=text)], trust=TrustLevel.PLATFORM, principal_id=None)


def _history_window(
    breakpoints: Sequence[CacheBreakpoint],
    *,
    conversation: Sequence[ConversationItem],
    carried_through: int | None,
    stable_through: int | None,
) -> list[CacheBreakpoint]:
    """Place a plan's history window on one request's conversation (Section 10.1).

    The window is two markers. One closes the history carried from earlier runs,
    protecting it even before this run completes an exchange. The other closes
    the longest prefix the next step of
    this run repeats, which stops before fresh step context or a replayed
    provider continuation: the next step refreshes the context and no longer
    carries those reasoning items. A marker past either point would write a
    cache entry that no later request reads.
    """
    placed: list[CacheBreakpoint] = []
    for hint in breakpoints:
        if hint.boundary != "after_history_prefix":
            placed.append(hint.model_copy(deep=True))
            continue
        for through in dict.fromkeys(
            index for index in (carried_through, stable_through) if index is not None
        ):
            input_through = next(
                (
                    index
                    for index in range(through, -1, -1)
                    if isinstance(conversation[index], (UserMessage, ToolResultItem))
                ),
                None,
            )
            placed.append(
                hint.model_copy(
                    update={
                        "through_item": through,
                        "through_input_item": input_through,
                    }
                )
            )
    return placed


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
            context_view(item)
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
        # The runtime row moves to the end of the body on a tool step, so the
        # prefix the next request repeats stops before it or any reasoning item.
        stable = next(
            index
            for index, item in enumerate(body)
            if item is runtime_item or isinstance(item, ProviderReasoningItem)
        )
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
                "context_origin_trust": TrustLevel.USER.value,
            },
            cache_hints=CacheHints(
                session_key=session_cache_key(run.tenant_id, run.session_id),
                breakpoints=_history_window(
                    [
                        CacheBreakpoint(boundary="after_system"),
                        CacheBreakpoint(boundary="after_tools"),
                        CacheBreakpoint(boundary="after_history_prefix"),
                    ],
                    conversation=[*prefix, *body],
                    carried_through=None,
                    stable_through=len(prefix) + stable - 1 if stable else None,
                ),
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
        memory_retriever: MemoryRetriever | None = None,
        query_former: QueryFormer | None = None,
        session_scope: Callable[[UUID], Awaitable[str]] | None = None,
        recall_revision: Callable[[], Awaitable[int | None]] | None = None,
        inline_output_bytes: int = 4096,
        catalog_source: RecordsCatalogSource | None = None,
        catalog_max_series: int = CATALOG_MAX_SERIES,
    ) -> None:
        self._planner = planner
        self._catalog_source = catalog_source
        if catalog_max_series < 0:
            raise ValueError("records catalog series limit must be nonnegative")
        self._catalog_max_series = catalog_max_series
        self._estimator = estimator
        self._clock = clock
        self._working_state = working_state
        self._memory_retriever = memory_retriever
        self._query_former = query_former
        self._session_scope = session_scope
        self._recall_revision = recall_revision
        if inline_output_bytes < 1024:
            raise ValueError("inline output byte limit must be at least 1024")
        self._inline_output_bytes = inline_output_bytes
        self._recall_tasks: OrderedDict[tuple[UUID, int, str], asyncio.Task[_RecallBundle]] = (
            OrderedDict()
        )

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
        prefix = build_prefix(
            agent,
            plan.tool_specs,
            plan.skill_catalog,
            plan.memory_snapshot,
            persona=plan.persona_text,
            deferred_tools=plan.deferred_tool_specs,
        )
        actual_prefix_hash = hashlib.sha256(
            prefix_bytes(prefix, plan.tool_specs, plan.deferred_tool_specs)
        ).hexdigest()
        if actual_prefix_hash != plan.prefix_sha256:
            raise ContextOverflow("the frozen context prefix no longer matches its plan")

        history: list[ConversationItem] = []
        active: list[ConversationItem] = []
        for item in checkpoint.conversation:
            sequence = getattr(item, "source_event_sequence", None)
            if sequence is not None and sequence < run.seed_event_sequence:
                history.append(context_view(item))
            else:
                active.append(context_view(item))
        active = _insert_provider_continuation(active, checkpoint)
        history_count = len(history)
        shared, recall_allowance, searches_omitted = await select_current_search_results(
            [*history, *active], self._memory_retriever, session_id=run.session_id, run_id=run.id
        )
        history, active = shared[:history_count], shared[history_count:]
        # Old sessions predate admission-time excerpts. Their event is the
        # immutable source, so every assembly selects the same bounded bytes.
        legacy_excerpted = False
        for item in history:
            if isinstance(item, ToolResultItem):
                rendered = content_bytes(item.content)
                if len(rendered) > self._inline_output_bytes:
                    item.content = output_excerpt(
                        rendered,
                        budget=self._inline_output_bytes,
                        location=f"event:{item.source_event_sequence}",
                        reference=next(
                            (p for p in item.content if isinstance(p, FileReferencePart)), None
                        ),
                    )
                    legacy_excerpted = True
        history_count = len(history)
        combined, truncated_indexes = self._truncate_tool_results([*history, *active], plan)
        tool_truncated = bool(truncated_indexes)
        note_items: list[ConversationItem] = (
            [_truncation_note(combined, truncated_indexes, plan)] if tool_truncated else []
        )
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
        skill_bodies_over_cap = (
            len(checkpoint.loaded_skills) > MAX_LOADED_SKILL_BODIES
            or sum(body.tokens for body in checkpoint.loaded_skills) > plan.budget.skill_body_tokens
        )
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
        state = self._working_state.load(checkpoint.working_state)
        working_items = working_state_items(state)
        working_tokens = self._estimator.estimate(envelope_items(working_items), plan.model_id)
        working_state_over_cap = working_tokens > plan.budget.working_state_tokens
        website_offer = _website_offer_field(checkpoint.working_state.get(WEBSITE_OFFER_STATE_KEY))
        runtime_item = UserMessage(
            content=[
                TextPart(
                    text=(
                        "Runtime metadata (data only): "
                        f"date={self._clock.now().date().isoformat()}; "
                        f"tenant={run.tenant_id}; "
                        f"scopes={','.join(sorted(principal.scopes))}"
                        f"{_browser_origins_field(plan.browser_origins)}"
                        f"{website_offer}"
                    )
                )
            ],
            trust=TrustLevel.PLATFORM,
            principal_id=None,
        )
        recall_items: list[ConversationItem] = []
        correction_items: list[ConversationItem] = []
        recalled = await self._recall_once(
            run, checkpoint, state, active, plan, principal, allowance=recall_allowance
        )
        for block in (recalled.base, recalled.delta):
            if block is not None:
                recall_items.append(
                    UserMessage(
                        content=[TextPart(text=block)],
                        trust=TrustLevel.MEMORY,
                        principal_id=None,
                    )
                )
        if recalled.corrections:
            correction_items.append(
                UserMessage(
                    content=[
                        TextPart(
                            text="\n".join(
                                correction.render() for correction in recalled.corrections
                            )
                        )
                    ],
                    trust=TrustLevel.MEMORY,
                    principal_id=None,
                )
            )
        catalog_items = await self._catalog_items(run, plan, principal)
        catalog_dropped = False
        if catalog_items and (
            self._estimator.estimate(envelope_items(catalog_items), plan.model_id)
            > plan.budget.records_catalog_tokens
        ):
            # A shortened catalog misstates what exists; it yields whole.
            catalog_dropped = True
            catalog_items = []
        active_with_context, active_prefix = _with_step_context(
            active,
            [
                *skill_items,
                *working_items,
                *recall_items,
                *correction_items,
                *catalog_items,
                runtime_item,
                *note_items,
            ],
        )
        fixed_body = [*summary_items, *active_with_context]
        fixed_tokens = self._estimator.estimate(envelope_items(fixed_body), plan.model_id)
        fixed_total = plan.prefix_tokens + fixed_tokens + plan.budget.reserve_output_tokens
        if catalog_items and fixed_total > plan.budget.total_tokens:
            # The catalog yields before in-turn recall trims: one records.list
            # call recovers it.
            catalog_dropped = True
            catalog_items = []
            active_with_context, active_prefix = _with_step_context(
                active,
                [
                    *skill_items,
                    *working_items,
                    *recall_items,
                    *correction_items,
                    runtime_item,
                    *note_items,
                ],
            )
            fixed_body = [*summary_items, *active_with_context]
            fixed_tokens = self._estimator.estimate(envelope_items(fixed_body), plan.model_id)
            fixed_total = plan.prefix_tokens + fixed_tokens + plan.budget.reserve_output_tokens
        recall_dropped = False
        if recall_items and fixed_total > plan.budget.total_tokens:
            # Recall yields; the corrections do not. They override the frozen
            # snapshot the prefix goes on rendering, so a long conversation
            # must never be able to squeeze one out — the never-yield set in
            # the context engine's "Yield order under pressure".
            recall_dropped = True
            recall_items = []
            active_with_context, active_prefix = _with_step_context(
                active,
                [
                    *skill_items,
                    *working_items,
                    *correction_items,
                    *catalog_items,
                    runtime_item,
                    *note_items,
                ],
            )
            fixed_body = [*summary_items, *active_with_context]
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
        if note_items:
            # The note names results still shown, so it is rendered again from
            # what the cut kept. It can only lose entries, so the fixed body it
            # was measured in still bounds it (ADR-0258).
            note_index = len(active_with_context) - (len(active) - active_prefix) - 1
            active_with_context[note_index] = _truncation_note(
                combined[cut:], {index - cut for index in truncated_indexes if index >= cut}, plan
            )
        retained_conversation = [*retained_history, *active_with_context]
        validate_tool_pairs(retained_conversation)

        yield_steps: list[str] = ["legacy_tool_excerpts"] if legacy_excerpted else []
        if searches_omitted:
            yield_steps.append("memory_searches")
        if tool_truncated:
            yield_steps.append("tool_results")
        if cut:
            yield_steps.append("history")
        if recall_dropped:
            yield_steps.insert(0, "recall")
        if catalog_dropped:
            yield_steps.insert(0, "records_catalog")

        body = [
            *summary_items,
            *retained_history,
            *active_with_context,
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
        fits = (
            not skill_bodies_over_cap
            and not working_state_over_cap
            and not excluded_unsummarized
            and not over_capacity
        )
        compactable = (
            not skill_bodies_over_cap
            and not working_state_over_cap
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
        elif skill_bodies_over_cap:
            reason = "skill_bodies_exceed_cap"
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
        carried = len(summary_items) + len(retained_history)
        stable = min(
            carried + active_prefix,
            next(
                (
                    index
                    for index, item in enumerate(rendered_body)
                    if isinstance(item, ProviderReasoningItem)
                ),
                len(rendered_body),
            ),
        )
        request = ModelRequest(
            model_policy=agent.model_policy,
            conversation=[*prefix, *rendered_body],
            recall_ranking=context_ranking(body, () if recall_dropped else recalled.rankings),
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
                "context_origin_trust": (
                    TrustLevel.MEMORY.value
                    if plan.memory_snapshot or recall_items or correction_items
                    else TrustLevel.USER.value
                ),
            },
            cache_hints=CacheHints(
                session_key=session_cache_key(run.tenant_id, run.session_id),
                breakpoints=_history_window(
                    plan.cache_breakpoints,
                    conversation=[*prefix, *rendered_body],
                    carried_through=len(prefix) + carried - 1 if carried else None,
                    stable_through=len(prefix) + stable - 1 if stable else None,
                ),
            ),
        )
        return ContextAssembly(request=request, pressure=pressure)

    async def _catalog_items(
        self, run: Run, plan: ContextPlan, principal: Principal
    ) -> list[ConversationItem]:
        """Read the owner's records catalog once per assembly, as one body item.

        Absent without a source (the flag is off), without `records.read`,
        without a configured class (zero caps), or when the store holds no
        series, so such a body keeps today's bytes and reads nothing.
        """

        if (
            self._catalog_source is None
            or CATALOG_SCOPE not in principal.scopes
            or plan.budget.records_catalog_tokens == 0
            or self._catalog_max_series == 0
        ):
            return []
        try:
            catalog = await self._catalog_source.catalog(
                principal,
                ceiling=Sensitivity.RESTRICTED,
                max_series=self._catalog_max_series,
            )
        except Exception as exc:
            logger.warning(
                "context_records_catalog_failed",
                extra={"run_id": str(run.id), "error_class": type(exc).__name__},
            )
            return []
        if catalog is None or (not catalog.lines and not catalog.omitted):
            return []
        text = render_records_catalog(catalog, zone=CATALOG_ZONE, now=self._clock.now())
        return [catalog_item(text)]

    async def _recall_once(
        self,
        run: Run,
        checkpoint: RunCheckpoint,
        state: WorkingState,
        active: list[ConversationItem],
        plan: ContextPlan,
        principal: Principal,
        *,
        allowance: RecallAllowance = FULL_ALLOWANCE,
    ) -> _RecallBundle:
        if self._memory_retriever is None or self._query_former is None:
            return _RecallBundle()
        message = current_user_text(active)
        if message is None:
            return _RecallBundle()
        # A checkpoint identifies a question, not the continued validity of its
        # evidence. Only a complete, trusted dependency revision may opt into
        # reuse. A store watermark alone misses expiry and optional-channel
        # admission/cache changes. Production therefore reads on every assembly.
        revision = None
        if self._recall_revision is not None:
            try:
                revision = await self._recall_revision()
            except Exception as exc:
                logger.warning(
                    "context_memory_revision_failed",
                    extra={"run_id": str(run.id), "error_class": type(exc).__name__},
                )
        if revision is None:
            for old_key in list(self._recall_tasks):
                if old_key[0] == run.id:
                    self._recall_tasks.pop(old_key)
            # Do not detach a task: cancellation owns and stops this read.
            return await self._compute_recall(
                run.model_copy(deep=True),
                state.model_copy(deep=True),
                message,
                snapshot_id=plan.snapshot_id,
                snapshot_watermark=plan.snapshot_watermark,
                allowance=allowance,
            )
        checkpoint_identity = hashlib.sha256(
            _canonical_json(
                {
                    "principal": principal.model_dump(mode="json"),
                    "run": run.model_dump(mode="json"),
                    "checkpoint": checkpoint.model_dump(mode="json"),
                    "last_event_sequence": checkpoint.last_event_sequence,
                    "conversation": [item.model_dump(mode="json") for item in active],
                    "working_state": state.model_dump(mode="json"),
                    # The plan's snapshot identity is part of what a cached
                    # recall answers: a rotated plan freezes a new snapshot at
                    # a new watermark, and the delta it bounds is a different
                    # question with the same conversation.
                    "epoch": plan.epoch,
                    "snapshot_id": None if plan.snapshot_id is None else str(plan.snapshot_id),
                    "snapshot_watermark": plan.snapshot_watermark,
                    "recall_revision": revision,
                    "recall_allowance": (allowance.tokens, allowance.items),
                }
            )
        ).hexdigest()
        key = (run.id, run.step_count, checkpoint_identity)
        task = self._recall_tasks.get(key)
        if task is None:
            task = asyncio.create_task(
                self._compute_recall(
                    run.model_copy(deep=True),
                    state.model_copy(deep=True),
                    message,
                    snapshot_id=plan.snapshot_id,
                    snapshot_watermark=plan.snapshot_watermark,
                    allowance=allowance,
                )
            )
            self._recall_tasks[key] = task
        self._recall_tasks.move_to_end(key)
        result = await asyncio.shield(task)
        while len(self._recall_tasks) > RECALL_CACHE_CAPACITY:
            oldest_key, oldest = next(iter(self._recall_tasks.items()))
            if not oldest.done():
                break
            self._recall_tasks.pop(oldest_key)
        return result

    async def _compute_recall(
        self,
        run: Run,
        state: WorkingState,
        message: str,
        *,
        snapshot_id: UUID | None,
        snapshot_watermark: int,
        allowance: RecallAllowance = FULL_ALLOWANCE,
    ) -> _RecallBundle:
        """Take the turn's base recall, and over a frozen snapshot its delta.

        The base recall is the turn's own question. The delta is that query
        taken again with no text over the store positions the session has not
        seen, which is what reaches a belief formed or corrected since the
        snapshot froze; the corrections are what override the snapshot members
        that stopped holding. Both further reads are skipped when the store
        head the base recall read is still the session's watermark, since
        nothing has been written since and neither could say anything. The
        delta also shares the base block's budget class rather than doubling
        it. Dependency-aware retrievers still validate frozen support when no
        position moved, including after derived recall is disabled.
        A failure in either read costs the turn only what that read would
        have added: the base recall it already has is still the turn's memory.
        """

        assert self._memory_retriever is not None
        assert self._query_former is not None
        seconds = AUTOMATIC_RECALL_SECONDS
        if run.deadline_at is not None:
            seconds = min(seconds, max(0, (run.deadline_at - self._clock.now()).total_seconds()))
        deadline = asyncio.get_running_loop().time() + seconds

        async def recall(query: RecallQuery) -> RecallResult:
            # Base and delta share one monotonic allowance, including time spent
            # between them. Mandatory snapshot corrections are not optional reads.
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError
            assert self._memory_retriever is not None
            async with asyncio.timeout_at(deadline):
                return await self._memory_retriever.recall(
                    query,
                    session_id=run.session_id,
                    run_id=run.id,
                    turn_id=run.id,
                    moment="in_turn",
                )

        try:
            # Recall is scoped to the session's project: a belief learned in
            # another one is carried in and demoted, never silently local.
            async with asyncio.timeout_at(deadline):
                scope = (
                    None
                    if self._session_scope is None
                    else await self._session_scope(run.session_id)
                )
            queries = [
                bounded
                for query in self._query_former.form(run, state, message, current_scope=scope)
                if (bounded := allowance.bound(query)) is not None
            ]
            if not queries:
                exhausted_corrections = (
                    []
                    if snapshot_id is None
                    else await self._memory_retriever.corrections(
                        snapshot_id=snapshot_id, watermark=snapshot_watermark
                    )
                )
                return _RecallBundle(corrections=tuple(exhausted_corrections))
            base = await recall(queries[0])
        except (RecallBudgetExceededError, TimeoutError) as error:
            limitation = (
                EXHAUSTED if isinstance(error, RecallBudgetExceededError) else RECALL_TIMED_OUT
            )
            # Optional search exhaustion cannot hide mandatory frozen corrections.
            try:
                exhausted_corrections = (
                    []
                    if snapshot_id is None
                    else await self._memory_retriever.corrections(
                        snapshot_id=snapshot_id, watermark=snapshot_watermark
                    )
                )
            except Exception as exc:
                logger.warning(
                    "context_memory_recall_failed",
                    extra={"run_id": str(run.id), "error_class": type(exc).__name__},
                )
                return _RecallBundle(base=limitation)
            return _RecallBundle(base=limitation, corrections=tuple(exhausted_corrections))
        except Exception as exc:
            logger.warning(
                "context_memory_recall_failed",
                extra={"run_id": str(run.id), "error_class": type(exc).__name__},
            )
            return _RecallBundle()
        rendered_base = base.rendered if base.items or base.people else None
        base_rankings = () if rendered_base is None else (ranking_block(base),)
        # One instant governs the turn's memory, the way it governs one recall:
        # a historical query stamps both blocks with the moment it asks about.
        instant = queries[0].as_of or self._clock.now()
        if snapshot_id is None or (
            base.watermark <= snapshot_watermark and not base.validate_snapshot_dependencies
        ):
            # Ordinary atom closures take a new position. Membership-aware
            # recall additionally validates elapsed dependency expiry even when
            # maintenance has not yet written an invalidation position.
            return _RecallBundle(base=rendered_base, rankings=base_rankings)
        # The two blocks share one budget class. In-turn recall is capped once
        # in the context engine's Region B table, not once per block, so the
        # delta is issued for what the base block left of it and is not issued
        # at all when the base block spent the class. Corrections are outside
        # this: they are unbudgeted, never yield, and are the trust-critical
        # half of the pair.
        remaining = queries[0].budget_tokens - base.tokens
        remaining_items = queries[0].max_items - len(base.items) - len(base.people)
        carried = {item.belief_id for item in base.items}
        corrections: list[MemoryCorrection] = []
        try:
            # Membership can expire without a source write. Validate frozen
            # dependencies before querying the delta so invalidation is visible.
            corrections = await self._memory_retriever.corrections(
                snapshot_id=snapshot_id,
                watermark=snapshot_watermark,
            )
            delta = (
                None
                if remaining <= 0 or remaining_items <= 0
                else await recall(
                    queries[0].model_copy(
                        update={
                            "profile": RecallProfile.CORE,
                            "text": None,
                            "subjects": [],
                            "structured_belief_types": [],
                            "include_owned_resources": False,
                            "include_home_location": False,
                            "min_store_position": snapshot_watermark,
                            "budget_tokens": remaining,
                            "max_items": remaining_items,
                            "exclude_ids": tuple(sorted(set(queries[0].exclude_ids) | carried)),
                        }
                    ),
                )
            )
        except Exception as exc:
            logger.warning(
                "context_memory_delta_failed",
                extra={"run_id": str(run.id), "error_class": type(exc).__name__},
            )
            # Corrections already fetched remain authoritative if optional delta recall fails.
            return _RecallBundle(
                base=rendered_base, corrections=tuple(corrections), rankings=base_rankings
            )
        # A belief the base recall already carries is not news, however new its
        # position is: stating it twice in one turn is two voices on one fact.
        fresh = (
            [] if delta is None else [item for item in delta.items if item.belief_id not in carried]
        )
        # Preserve the actual retriever block when its exclusions already did
        # the deduplication. Re-rendering changed its instant and policy stamp,
        # preventing the runtime observation from identifying visible evidence.
        unchanged_delta = delta is not None and fresh == delta.items and not delta.people
        rendered_delta = (
            delta.rendered
            if fresh and unchanged_delta and delta is not None
            else render_memory(fresh, as_of=instant)
            if fresh
            else None
        )
        delta_rankings = (
            ()
            if rendered_delta is None
            else (ranking_block(delta) if unchanged_delta and delta is not None else None,)
        )
        return _RecallBundle(
            base=rendered_base,
            delta=rendered_delta,
            corrections=tuple(corrections),
            rankings=(*base_rankings, *delta_rankings),
        )

    def _truncate_tool_results(
        self,
        items: list[ConversationItem],
        plan: ContextPlan,
    ) -> tuple[list[ConversationItem], set[int]]:
        """Truncate the oldest tool results to pointers, in budget-quarter blocks.

        The minimal truncation that fits the aggregate budget would move by one
        result on every step of a long tool loop, rewriting the oldest bytes of
        the body on each request and defeating the provider's cache of
        everything behind them. So once truncation is needed, it continues to
        the next quarter-budget boundary of truncated tokens (ADR-0242). The
        truncated prefix then changes once per quarter budget of new results,
        and the function stays a pure function of the items it is given.

        The newest definition answer of each deferred tool stays whole and
        still counts against the budget, while the kept answers together fit
        it, and with the pointers of everything else once nothing else is
        whole (ADR-0258). Returns the indexes it truncated.
        """

        result = [item.model_copy(deep=True) for item in items]
        budget = plan.budget.tool_result_tokens
        # Kept answers together stay within the budget, newest first; an older
        # one past it truncates in its turn like any other result.
        kept: set[int] = set()
        kept_tokens = 0
        for index in sorted(_definition_answers(result, plan), reverse=True):
            kept_tokens += self._estimator.estimate([result[index]], plan.model_id)
            if kept_tokens > budget:
                break
            kept.add(index)
        definitions = [result[index] for index in sorted(kept)]
        indexes = [
            index
            for index, item in enumerate(result)
            if isinstance(item, ToolResultItem) and index not in kept
        ]
        block = max(1, budget // TOOL_RESULT_TRUNCATION_BLOCKS)
        truncated: set[int] = set()
        truncated_tokens = 0
        target_tokens: int | None = None
        while indexes:
            if target_tokens is None:
                tool_items = [*definitions, *(result[index] for index in indexes)]
                if self._estimator.estimate(tool_items, plan.model_id) <= budget:
                    if truncated:
                        target_tokens = -(-truncated_tokens // block) * block
                        continue
                    break
            elif truncated_tokens >= target_tokens:
                break
            index = indexes.pop(0)
            item = result[index]
            assert isinstance(item, ToolResultItem)
            truncated_tokens += self._estimator.estimate([item], plan.model_id)
            result[index] = _tool_result_pointer(item)
            truncated.add(index)
        # Once every other result is a pointer, the pointers count as well: if
        # they and the kept answers still pass the budget, the oldest answers
        # give way too.
        while not indexes and kept:
            shown = [item for item in result if isinstance(item, ToolResultItem)]
            if self._estimator.estimate(shown, plan.model_id) <= budget:
                break
            index = min(kept)
            kept.remove(index)
            item = result[index]
            assert isinstance(item, ToolResultItem)
            result[index] = _tool_result_pointer(item)
            truncated.add(index)
        return result, truncated


# ADR-0130: the runtime metadata row names at most this many browser origins.
MAXIMUM_RENDERED_BROWSER_ORIGINS = 16


def _website_offer_field(raw: object) -> str:
    """ADR-0251: what a chat without the named website can do about it.

    Facts only, from the admitted checkpoint; never a profile ID.
    """

    if not isinstance(raw, dict):
        return ""
    offered = [origin for origin in raw.get("offered", ()) if isinstance(origin, str)]
    connected = raw.get("connected")
    if not offered:
        if connected is not None:
            return ""
        return (
            "; website=none (no saved website login is connected to this chat; "
            "the owner connects one from Website Access in this chat)"
        )
    shown = ",".join(offered[:MAXIMUM_RENDERED_BROWSER_ORIGINS])
    if isinstance(connected, str):
        where = f"this chat is connected to {connected} instead"
        button = "a Switch button that moves this chat to it"
    else:
        where = "not connected to this chat"
        button = "a Connect button that connects it to this chat"
    return (
        f"; website_offer={shown} (a saved website login the owner named; {where}; "
        f"the app shows the owner {button} and continues this request)"
    )


def _browser_origins_field(origins: tuple[str, ...]) -> str:
    """The origins browser.navigate accepts, or nothing for a chat without them."""

    if not origins:
        return ""
    shown = ",".join(origins[:MAXIMUM_RENDERED_BROWSER_ORIGINS])
    hidden = len(origins) - MAXIMUM_RENDERED_BROWSER_ORIGINS
    return f"; browser_origins={shown}" + (f",+{hidden} more" if hidden > 0 else "")
