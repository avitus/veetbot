"""Observe a controlled reader through the actual runtime and context builder.

Only fabricated, isolated cases enter this driver. Default deliberate recall scripts
one memory.search call; a separate option lets the reader plan one search before
answering. Automatic recall uses the production People-aware context composition.
The PostgreSQL mode executes the real durable worker and run authority. In-memory
mode remains a separate diagnostic and does not certify production latency.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

from agent_core.config import load_config_document
from agent_core.context.estimator import ConservativeTokenEstimator
from agent_core.context.working_state import WorkingStateManager
from agent_core.domain.context import WorkingState
from agent_core.domain.events import NewEvent
from agent_core.domain.general_recall_release import PathKind
from agent_core.domain.local_embeddings import LocalEmbeddingConfig
from agent_core.domain.memory import RecallQuery, RecallResult
from agent_core.domain.messages import (
    FakeModelScript,
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelRequest,
    ResolvedModel,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
    SystemMessage,
    TextPart,
    UserMessage,
)
from agent_core.domain.model_settings import ModelSettingsCatalog, chat_reasoning_effort
from agent_core.domain.runs import TERMINAL_RUN_STATUSES, Run, RunCheckpoint
from agent_core.domain.semantic_reader import reader_identity
from agent_core.domain.semantic_work import EmbeddingConfig
from agent_core.evals.general_recall_composition import scripted_provider
from agent_core.evals.general_recall_corpus import TaskInput
from agent_core.evals.general_recall_reader import reader_request
from agent_core.memory.local_query_authority import LocalQueryAuthority
from agent_core.memory.profiles import RetrievalProfile
from agent_core.memory.recall_preparation import SemanticRecallPreparation
from agent_core.memory.retrieval import DeterministicQueryFormer, HybridMemoryRetriever
from agent_core.memory.semantic_queries import SemanticQueryPreparation
from agent_core.memory.semantic_runtime import RunSemanticQueries
from agent_core.model import NON_ROUTED_MODEL_POLICIES
from agent_core.ports.embeddings import EmbeddingProvider
from agent_core.ports.models import ModelProvider, ModelRouter
from agent_core.runtime.loop import RecallPreparation


def reader_runtime_inputs(
    resolved: ResolvedModel,
    *,
    catalog: ModelSettingsCatalog | None = None,
    router: ModelRouter | None = None,
) -> dict[str, Any]:
    """Describe the same reader identity the unmodified executor checkpoints."""
    effort = (
        None
        if catalog is None
        else chat_reasoning_effort(catalog.chat_default, resolved.reasoning_efforts)
    )
    version = (
        None
        if router is None or resolved.policy_name in NON_ROUTED_MODEL_POLICIES
        else router.pin(UUID(int=0), resolved).registry_version
    )
    return {
        "reasoning_effort": effort,
        "reasoning_summary": catalog is not None,
        "registry_version": version,
        "reader_sha256": reader_identity(
            resolved,
            reasoning_effort=effort,
            reasoning_summary=catalog is not None,
            registry_version=version,
        ),
    }


@dataclass(frozen=True)
class RuntimeReading:
    answer: str
    request: ModelRequest
    completed: ModelCompletedEvent
    status: str
    tool_calls: int
    checkpoint: RunCheckpoint
    planning_calls: int


def run_bound_queries(
    app: Any,
    provider: EmbeddingProvider,
    embedding: EmbeddingConfig,
    local: LocalEmbeddingConfig,
    profile: RetrievalProfile,
    resolved: ResolvedModel,
    *,
    recover_sources: bool,
    production_reader: bool = False,
) -> RunSemanticQueries:
    """Use the real authority and lease checks on explicitly isolated synthetic data."""
    return RunSemanticQueries(
        app.uow_factory,
        app.clock,
        app.principal,
        provider,
        embedding,
        admitted=lambda: True,
        local=local,
        authority=LocalQueryAuthority(
            app.uow_factory,
            app.principal,
            policy_version=app.ruleset.policy_version,
            retrieval_policy=hashlib.sha256(
                json.dumps(
                    {
                        "profile": profile.model_dump(mode="json"),
                        "source_recovery": recover_sources,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            reader_sha256=reader_runtime_inputs(
                resolved,
                catalog=app.executor._model_settings if production_reader else None,
                router=app.executor._model_router if production_reader else None,
            )["reader_sha256"],
        ),
    )


async def freeze_empty_task_context(app: Any, session_id: UUID, resolved: ResolvedModel) -> None:
    """Freeze the new chat before source histories arrive, without a gold snapshot."""
    async with app.uow_factory() as uow:
        session = await uow.sessions.get(session_id, app.principal)
        agent = await uow.agents.get_version(session.agent_id, session.agent_version)
    await app.executor._context_planner.plan(session, agent, app.principal, resolved)


class _Reader:
    def __init__(
        self,
        app: Any,
        task: TaskInput,
        path: PathKind,
        provider: ModelProvider,
        search_planning: Literal["scripted", "reader"],
    ) -> None:
        self.name = provider.name
        self.provider, self.task, self.path = provider, task, path
        self.requests: list[ModelRequest] = []
        self.completed: ModelCompletedEvent | None = None
        self.search_planning = search_planning
        self.planning_calls = 0
        self.script = scripted_provider(
            FakeModelScript(
                turns=[
                    ScriptedTurn(
                        tool_calls=[
                            ScriptedToolCall(
                                call_id="evaluation-search",
                                name="memory.search",
                                arguments={"text": task.question, "scope": task.scope},
                            )
                        ],
                        stop_reason=StopReason.TOOL_USE,
                    )
                ]
            ),
            app.clock,
        )
        self.search_sent = False

    async def close(self) -> None:
        return None  # The experiment owns the borrowed provider.

    async def stream(
        self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        if (
            self.path == "deliberate"
            and not self.search_sent
            and self.search_planning == "scripted"
        ):
            self.search_sent = True
            async for event in self.script.stream(request, resolved, attempt):
                yield event
            return
        allow_search = self.search_planning == "reader" and not self.requests
        if self.completed is not None or (
            self.requests
            and not (
                self.search_planning == "reader"
                and len(self.requests) == 1
                and self.planning_calls == 1
            )
        ):
            raise ValueError("A controlled answer cannot silently retry or make another call")
        format_request = reader_request(self.task, "", model_policy=resolved.policy_name)
        planning_messages = (
            [
                SystemMessage(
                    content=[
                        TextPart(
                            text=(
                                "Use at most one memory.search call to resolve missing "
                                "supporting evidence, then return the requested answer. "
                                "Search for information needs; never guess "
                                "a missing personal value. No other tool is allowed. "
                                + (
                                    "For this deliberate recall measurement, "
                                    "search before answering."
                                    if self.path == "deliberate"
                                    else "Search only if the available evidence is insufficient."
                                )
                            )
                        )
                    ]
                )
            ]
            if allow_search
            else []
        )
        # Preserve the real prefix, memory trust, working state and tool envelope.
        # Only the answer format is controlled, and it carries no expected values.
        actual = request.model_copy(
            update={
                "conversation": [
                    *request.conversation,
                    format_request.conversation[0],
                    *planning_messages,
                    UserMessage(
                        content=[
                            TextPart(
                                text=json.dumps(
                                    {
                                        "requested_output_fields": self.task.output_fields,
                                        "output_kind": self.task.output_kind,
                                    }
                                )
                            )
                        ]
                    ),
                ],
                "tools": [tool for tool in request.tools if tool.name == "memory.search"]
                if allow_search
                else [],
                "tool_choice": None if allow_search else "none",
                "response_schema": format_request.response_schema,
                "maximum_output_tokens": 1024,
                "maximum_provider_attempts": 1,
                "timeout_seconds": 30,
                "stream_idle_seconds": 15,
            }
        )
        self.requests.append(actual)
        completed_here = False
        async for event in self.provider.stream(actual, resolved, attempt):
            if isinstance(event, ModelCompletedEvent):
                if completed_here or event.internal_retry_count:
                    raise ValueError("The measured reader cannot retry or complete twice")
                completed_here = True
                if event.stop_reason == StopReason.TOOL_USE:
                    if (
                        not allow_search
                        or len(event.turn.tool_calls) != 1
                        or event.turn.tool_calls[0].name != "memory.search"
                    ):
                        raise ValueError("The reader may plan only one memory.search call")
                    self.planning_calls += 1
                    self.search_sent = True
                elif event.stop_reason == StopReason.END_TURN and not event.turn.tool_calls:
                    self.completed = event
                else:
                    raise ValueError("Reader must finish one ordinary answer or bounded search")
            yield event


class _Preparation:
    execution_budget = True

    def __init__(
        self,
        app: Any,
        retriever: HybridMemoryRetriever,
        lookup: SemanticQueryPreparation,
        former: DeterministicQueryFormer,
    ) -> None:
        self.former = SemanticRecallPreparation(app.uow_factory, app.principal, retriever, former)
        self.retriever, self.lookup = retriever, lookup

    async def plan(self, run: Run, checkpoint: RunCheckpoint) -> list[RecallQuery]:
        return await self.former.plan(run, checkpoint)

    async def prepare(self, run: Run, queries: Sequence[RecallQuery]) -> None:
        result = await self.lookup.prepare(
            [self.retriever._effective_query(query, "in_turn") for query in queries]
        )
        if result.prepared != len(queries) and result.reason != "cached":
            raise ValueError("Semantic evaluation preparation was unavailable")


class _DeliberatePreparation:
    """Isolate deliberate recall before either planning or reserving automatic work."""

    execution_budget = True

    def __init__(self, preparation: RecallPreparation) -> None:
        self.preparation = preparation

    async def plan(self, run: Run, checkpoint: RunCheckpoint) -> Sequence[RecallQuery]:
        if not checkpoint.pending_tool_calls:
            return []
        return await self.preparation.plan(run, checkpoint)

    async def prepare(self, run: Run, queries: Sequence[RecallQuery]) -> None:
        await self.preparation.prepare(run, queries)


async def read_through_runtime(
    app: Any,
    task: TaskInput,
    session_id: UUID,
    path: PathKind,
    retriever: HybridMemoryRetriever,
    lookup: SemanticQueryPreparation | None,
    provider: ModelProvider,
    resolved: ResolvedModel,
    *,
    durable: bool = False,
    runtime_bound: bool = False,
    search_planning: Literal["scripted", "reader"] = "scripted",
    production_reader: bool = False,
) -> RuntimeReading:
    # Existing builder, People service, tool registry and pipeline all retain this
    # shared instance. The experiment substitutes its measured retrieval policy.
    app.memory_retriever._profile = retriever.retrieval_profile
    app.memory_retriever.bind_semantic_queries = retriever.bind_semantic_queries
    app.memory_retriever.recall = retriever.recall
    app.memory_retriever.validate_retained = retriever.validate_retained
    former = DeterministicQueryFormer(
        app.principal,
        candidate_selection=retriever.retrieval_profile.candidate_selection,
        planning=retriever.retrieval_profile.query_planning,
        admitted=lambda: True,
    )
    app.executor._context_builder._query_former = former
    if path == "deliberate":
        # Isolate deliberate evidence use from an automatic answer to the same task.
        app.executor._context_builder._query_former = None
    if lookup is not None:
        app.executor._prepare_recall = _Preparation(app, retriever, lookup, former)
    else:
        app.executor._prepare_recall = SemanticRecallPreparation(
            app.uow_factory, app.principal, retriever, former
        )
    if path == "deliberate":
        app.executor._prepare_recall = _DeliberatePreparation(app.executor._prepare_recall)
    reader = _Reader(app, task, path, provider, search_planning)
    if production_reader:
        app.executor._model_providers = {**app.executor._model_providers, resolved.provider: reader}
    else:
        app.executor._model_router = None
        app.executor._model_settings = None
    app.executor._resolved_model = resolved
    app.executor._model_provider = reader
    if task.context_update is not None:
        manager = WorkingStateManager(
            app.clock,
            load_config_document(app.settings, "context/plan.yaml")["working_state"],
            ConservativeTokenEstimator(),
        )
        state = manager.transition(
            WorkingState(), task.context_update.model_dump(exclude_unset=True)
        )
        async with app.uow_factory() as uow:
            await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="context.working_state.updated",
                    actor_type="runtime",
                    payload={"working_state": state.model_dump(mode="json")},
                )
            )
    run_id = await app.runs.submit(task.question, session_id)
    worker = app.worker_factory("m33-evaluation") if durable else None
    async with asyncio.timeout(45):
        while (run := await app.runs.get(run_id)).status not in TERMINAL_RUN_STATUSES:
            if worker is not None:
                if not await worker.run_once():
                    raise ValueError("The evaluation worker could not claim its run")
                continue
            # Do not advance the fixed evaluation clock while waiting for network I/O.
            await asyncio.sleep(0.01)
    if (
        run.failure is not None
        or reader.completed is None
        or len(reader.requests) != 1 + reader.planning_calls
    ):
        raise ValueError(f"Runtime reader failed: {run.failure}")
    async with app.uow_factory() as uow:
        checkpoint = await uow.checkpoints.latest(run_id)
    assert checkpoint is not None
    answer = "\n".join(
        part.text
        for message in reader.completed.turn.assistant_messages
        for part in message.content
        if isinstance(part, TextPart)
    )
    return RuntimeReading(
        answer,
        reader.requests[-1],
        reader.completed,
        run.status.value,
        run.tool_call_count,
        checkpoint,
        reader.planning_calls,
    )


def visible_recall(
    results: Sequence[RecallResult], request: ModelRequest
) -> tuple[RecallResult, ...]:
    """Keep only evidence whose rendered bytes actually reached this answer request."""
    texts = [
        part.text
        for item in request.conversation
        for part in getattr(item, "content", ())
        if isinstance(part, TextPart)
    ]
    visible: dict[str, RecallResult] = {}
    if request.recall_ranking is not None:
        by_trace = {result.trace_id: result for result in results}
        for block in request.recall_ranking.blocks:
            result = by_trace.get(block.trace_id)
            if (
                result is None
                or hashlib.sha256(result.rendered.encode()).hexdigest() != block.content_sha256
                or not any(result.rendered in text for text in texts)
            ):
                raise ValueError("Runtime ranking receipt does not identify visible evidence")
            visible.setdefault(result.rendered, result)
    for result in results:
        if result.rendered and any(result.rendered in text for text in texts):
            visible.setdefault(result.rendered, result)
    return tuple(visible.values())
