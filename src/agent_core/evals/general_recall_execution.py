"""Isolated formation-to-reader measurements for general memory recall.

This driver measures actual calls, never activates production, and does not
manufacture release receipts. A live caller supplies a journaled provider budget.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack
from dataclasses import asdict, replace
from datetime import timedelta
from decimal import Decimal
from itertools import count
from math import ceil
from pathlib import Path
from time import perf_counter
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from agent_core.adapters.determinism import FixedClock, SequenceIdFactory
from agent_core.config import (
    DeploymentMode,
    MemoryFormationPolicyPin,
    MemoryProviderExtractionMode,
    Settings,
    load_config_document,
)
from agent_core.domain.agents import Principal
from agent_core.domain.events import NewEvent
from agent_core.domain.general_recall_release import Channel, PathKind, Pipeline
from agent_core.domain.local_embeddings import LOCAL_EMBEDDING_REVISION, LocalEmbeddingConfig
from agent_core.domain.memory import (
    BeliefType,
    MemoryAuthority,
    MemoryRecord,
    MemoryStatus,
    Portability,
    RecallQuery,
    RecallResult,
    Sensitivity,
)
from agent_core.domain.messages import (
    FakeModelScript,
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelRequest,
    ModelUsage,
    ResolvedModel,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.people import PeopleQuery
from agent_core.domain.policies import TrustLevel
from agent_core.domain.semantic_work import EmbeddingConfig
from agent_core.evals.general_memory_recall_driver import OWNER, tool_context
from agent_core.evals.general_recall_composition import fixed_clock, sequence_ids
from agent_core.evals.general_recall_confirmation import ConfirmedExecution
from agent_core.evals.general_recall_corpus import RecallFixture, score_answer, validate_corpus
from agent_core.evals.general_recall_database import EvaluationClock, evaluation_database
from agent_core.evals.general_recall_formation_bank import FormationBank
from agent_core.evals.general_recall_reader import reader_request
from agent_core.evals.general_recall_stages import (
    answer_violations,
    authored_source_evidence,
    evidence_labels,
    observed_ranked_ids,
    recovered_source_witnesses,
    rendered_noise_items,
)
from agent_core.evals.memory_formation import _evaluation_settings
from agent_core.evals.people_execution import BorrowedProvider, BudgetedProvider
from agent_core.memory.general_recall_evidence import source_labels
from agent_core.memory.profiles import MemoryProfiles
from agent_core.memory.reconsolidation_evidence import implementation_digest
from agent_core.memory.retrieval import DeterministicQueryFormer, HybridMemoryRetriever
from agent_core.memory.semantic_deployment import configuration_digest
from agent_core.memory.semantic_indexing import SemanticIndexingPass
from agent_core.memory.semantic_queries import SemanticQueryContext, SemanticQueryPreparation
from agent_core.memory.source_recovery import BoundedSourceRecovery
from agent_core.ports.embeddings import EmbeddingProvider
from agent_core.tools.memory_search import MemorySearchTool


def fixture_ids(case_id: str) -> SequenceIdFactory:
    namespace = uuid5(NAMESPACE_URL, f"m33-fixture:{case_id}")
    return sequence_ids(uuid5(namespace, str(index)) for index in count(1))


def _foreign_owner_id(principal: Principal) -> str:
    return (
        "synthetic-other-owner-2"
        if principal.principal_id == "synthetic-other-owner"
        else "synthetic-other-owner"
    )


async def _formed_state_digest(app: Any, records: list[MemoryRecord]) -> str:
    people: list[dict[str, Any]] = []
    kinds = [
        "person",
        "identifier",
        "source",
        "mention",
        "memory_link",
        "organization",
        "relationship",
        "interaction",
        "commitment",
        "operation",
        "merge_suggestion",
        "erasure",
        "import_job",
    ]
    async with app.uow_factory() as uow:
        for offset in range(0, len(kinds), 10):
            query = PeopleQuery(
                tenant_id=app.principal.tenant_id,
                principal_id=app.principal.principal_id,
                kinds=kinds[offset : offset + 10],
                sensitivity_ceiling=Sensitivity.RESTRICTED,
                include_superseded=True,
                limit=100,
            )
            for _ in range(100):
                page = await uow.people.query(query)
                people.extend(row.model_dump(mode="json") for row in page[:100])
                if len(page) <= 100:
                    break
                query = query.model_copy(update={"after": page[99].id})
            else:
                raise ValueError("The formation bank People state exceeds its comparison bound")
    # Absolute store positions can differ after a scratch reset. Their complete
    # relative order remains bound, along with every other stored belief field.
    state = {
        "memories": [
            record.model_dump(mode="json", exclude={"store_position"})
            for record in sorted(records, key=lambda row: (row.store_position, str(row.id)))
        ],
        "people": sorted(people, key=lambda row: str(row["id"])),
    }
    return hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()


class _ObservedRetriever(HybridMemoryRetriever):
    observed: RecallResult | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.results: list[RecallResult] = []
        self.elapsed_ms = 0.0
        self.executed_searches = 0

    async def _execute_recall(self, query: RecallQuery, **kwargs: Any) -> RecallResult:
        # The public method acquires the execution permit before entering here.
        # Failed admitted searches count; refused calls do not claim a DB read.
        self.executed_searches += 1
        return await super()._execute_recall(query, **kwargs)

    async def recall(self, query: RecallQuery, **kwargs: Any) -> RecallResult:
        started = perf_counter()
        self.observed = await super().recall(query, **kwargs)
        self.elapsed_ms += (perf_counter() - started) * 1000
        self.results.append(self.observed)
        return self.observed


class _MeasuredProvider(BorrowedProvider):
    def __init__(self, provider: BudgetedProvider) -> None:
        super().__init__(provider)
        self.usages: list[ModelUsage] = []
        self.calls = 0
        self.attempts: list[dict[str, Any]] = []

    async def stream(
        self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        self.calls += 1
        observed: dict[str, Any] = {
            "provider": resolved.provider,
            "model": resolved.model,
            "policy": resolved.policy_name,
            "completed": False,
            "request_sha256": hashlib.sha256(request.model_dump_json().encode()).hexdigest(),
        }
        self.attempts.append(observed)
        try:
            async for event in self.provider.stream(request, resolved, attempt):
                if isinstance(event, ModelCompletedEvent):
                    self.usages.append(event.turn.usage)
                    observed.update(completed=True, usage=event.turn.usage.model_dump(mode="json"))
                yield event
        except BaseException as error:
            observed["error_class"] = type(error).__name__
            raise


async def _history(
    app: Any, case: RecallFixture, *, form: bool
) -> tuple[dict[str, tuple[UUID, int]], list[dict[str, Any]]]:
    sessions: dict[tuple[str, str, str], UUID] = {}
    sources = {}
    formation_runs = []
    remaining = Counter(
        (turn.session, turn.scope, turn.speaker == "other_owner") for turn in case.history
    )
    for turn in case.history:
        app.clock.advance(turn.at - app.clock.now())
        owner = (
            app.principal
            if turn.speaker != "other_owner"
            else app.principal.model_copy(update={"principal_id": _foreign_owner_id(app.principal)})
        )
        key = (turn.session, turn.scope, owner.principal_id)
        if key not in sessions:
            view = await app.services.sessions.create(
                owner, "general", {"project_scope": turn.scope}
            )
            sessions[key] = view.id
        async with app.uow_factory() as uow:
            event = await uow.events.append(
                NewEvent(
                    session_id=sessions[key],
                    run_id=None,
                    event_type="assistant.message.created"
                    if turn.speaker == "assistant"
                    else "user.message.created",
                    actor_type="model" if turn.speaker == "assistant" else "principal",
                    actor_id=owner.principal_id,
                    payload={
                        "content": turn.content,
                        "trust": TrustLevel.EXTERNAL_UNTRUSTED.value
                        if turn.speaker == "external"
                        else TrustLevel.USER.value,
                    },
                )
            )
        sources[turn.id] = (event.session_id, event.sequence)
        count_key = (turn.session, turn.scope, turn.speaker == "other_owner")
        remaining[count_key] -= 1
        if form and remaining[count_key] == 0 and turn.speaker != "other_owner":
            result = await app.memory.run(
                trigger="evaluation", scope=turn.scope, session_id=event.session_id
            )
            formation_runs.append(result.model_dump(mode="json"))
    return sources, formation_runs


async def _seed(app: Any, case: RecallFixture, sources: dict[str, tuple[UUID, int]]) -> None:
    turns = {turn.id: turn for turn in case.history}
    # Seed the authored current snapshot without deferring or weakening foreign
    # keys. Replacement originals must exist before their superseded predecessors.
    pending = list(case.evidence)
    ordered = []
    present: set[str] = set()
    while pending:
        ready = [
            atom for atom in pending if atom.superseded_by is None or atom.superseded_by in present
        ]
        if not ready:
            raise ValueError("Seeded replacement dependencies are incomplete or cyclic")
        ordered.extend(ready)
        present.update(atom.id for atom in ready)
        pending = [atom for atom in pending if atom.id not in present]
    async with app.uow_factory() as uow:
        for atom in ordered:
            source = turns[atom.source_ids[0]]
            session_id, _ = sources[source.id]
            if any(sources[key][0] != session_id for key in atom.source_ids):
                raise ValueError("A seeded original requires one source session")
            at = max(turns[key].at for key in atom.source_ids)
            await uow.memories.upsert_belief(
                MemoryRecord(
                    id=uuid5(NAMESPACE_URL, f"m33:{case.id}:{atom.id}"),
                    tenant_id=app.principal.tenant_id,
                    principal_id=_foreign_owner_id(app.principal)
                    if source.speaker == "other_owner"
                    else app.principal.principal_id,
                    subject=atom.subject,
                    statement=atom.statement,
                    scope=source.scope,
                    origin_scopes=[source.scope],
                    source_session_id=session_id,
                    source_event_ids=[sources[key][1] for key in atom.source_ids],
                    belief_type=atom.belief_type,
                    claim_kind=atom.claim_kind,
                    status=atom.status,
                    portability=atom.portability,
                    sensitivity=atom.sensitivity,
                    authority=MemoryAuthority.USER,
                    confidence=0.9,
                    valid_from=at,
                    valid_to=atom.valid_to,
                    superseded_by=None
                    if atom.superseded_by is None
                    else uuid5(NAMESPACE_URL, f"m33:{case.id}:{atom.superseded_by}"),
                    last_evidence_at=at,
                    last_reinforced_at=at,
                    created_at=at,
                    updated_at=at,
                    formation_run_id=uuid5(session_id, "seeded"),
                    consolidation_policy_version="m33-seeded@1",
                    store_position=await uow.memories.next_position(),
                )
            )


async def _noise(app: Any, case: RecallFixture, count: int) -> None:
    # Load variants are not independent scenarios. They are separate newer
    # unrelated facts; authored hard distractors belong in each case's history.
    source = next((turn for turn in case.history if turn.speaker == "owner"), None)
    if source is None:
        if count:
            raise ValueError("Load noise needs an owner session")
        return
    app.clock.advance(
        max(app.clock.now(), case.task.at - timedelta(microseconds=1)) - app.clock.now()
    )
    view = await app.services.sessions.create(
        app.principal, "general", {"project_scope": case.task.scope}
    )
    session_id = view.id
    async with app.uow_factory() as uow:
        for index in range(count):
            statement = f"User requested project ticket {index} for the next review."
            event = await uow.events.append(
                NewEvent(
                    session_id=session_id,
                    run_id=None,
                    event_type="user.message.created",
                    actor_type="principal",
                    actor_id=app.principal.principal_id,
                    payload={"content": statement},
                )
            )
            await uow.memories.upsert_belief(
                MemoryRecord(
                    id=uuid5(NAMESPACE_URL, f"m33:{case.id}:noise:{index}"),
                    tenant_id=app.principal.tenant_id,
                    principal_id=app.principal.principal_id,
                    scope=case.task.scope,
                    subject=f"Project ticket {index}",
                    statement=statement,
                    origin_scopes=[case.task.scope],
                    source_session_id=session_id,
                    source_event_ids=[event.sequence],
                    confidence=0.9,
                    sensitivity=Sensitivity.INTERNAL,
                    belief_type=BeliefType.FACT,
                    portability=Portability.CONTEXTUAL,
                    status=MemoryStatus.ACTIVE,
                    authority=MemoryAuthority.USER,
                    valid_from=case.task.at,
                    last_evidence_at=case.task.at,
                    last_reinforced_at=case.task.at,
                    created_at=case.task.at,
                    updated_at=case.task.at,
                    formation_run_id=uuid5(session_id, "noise"),
                    consolidation_policy_version="m33-noise@1",
                    store_position=await uow.memories.next_position(),
                )
            )


async def evaluate_fixture(
    *,
    case: RecallFixture,
    settings: Settings,
    pipeline: Pipeline,
    channel: Channel,
    path: PathKind,
    reader: BudgetedProvider,
    resolved: ResolvedModel,
    noise: int,
    local: LocalEmbeddingConfig | None = None,
    embeddings: EmbeddingProvider | None = None,
    recover_sources: bool = False,
    semantic_fusion_weight: float = 0.2,
    formation_policy: Literal["formation@9", "formation@11"] = "formation@11",
    execution: Literal["runtime", "direct"] = "runtime",
    reranking: Literal["baseline", "relevance"] = "baseline",
    query_planning: Literal["baseline", "task_context"] = "baseline",
    search_planning: Literal["scripted", "reader"] = "scripted",
    storage: Literal["memory", "postgres"] = "memory",
    disposable_database: bool = False,
    formation_bank_path: Path | None = None,
    historical_indexing: bool = False,
    production_reader: bool = False,
    principal: Principal = OWNER,
    validate_composition: Callable[[dict[str, Any]], None] | None = None,
    confirmation: ConfirmedExecution | None = None,
) -> dict[str, Any]:
    from agent_core.evals.general_recall_composition import build
    from agent_core.evals.general_recall_runtime import (
        freeze_empty_task_context,
        read_through_runtime,
        reader_runtime_inputs,
        run_bound_queries,
        visible_recall,
    )

    if settings.deployment_mode is DeploymentMode.PRODUCTION or not 0 <= noise <= 10000:
        raise ValueError("M33 evaluation requires a bounded disposable environment")
    if storage == "postgres" and execution != "runtime":
        raise ValueError("PostgreSQL evaluation requires the actual runtime")
    if search_planning == "reader" and execution != "runtime":
        raise ValueError("Reader-planned search requires the actual runtime")
    if case.task.context_update is not None and execution != "runtime":
        raise ValueError("Carried task context requires the actual runtime")
    if formation_bank_path is not None and pipeline != "formation":
        raise ValueError("A formation bank requires the formation pipeline")
    if channel != "sparse" and (local is None or embeddings is None):
        raise ValueError("Semantic comparison requires an explicit offline embedding tuple")
    validate_corpus((case,))
    measured = _MeasuredProvider(reader)
    formation_started = perf_counter()
    async with AsyncExitStack() as stack:
        await stack.enter_async_context(
            evaluation_database(settings, storage=storage, authorized=disposable_database)
        )
        temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="m33-fixture-"))
        effective = replace(
            _evaluation_settings(settings, Path(temporary)),
            people_enabled=formation_policy == "formation@11",
            memory_semantic_index_enabled=False,
            memory_dreaming_review_enabled=False,
            memory_reconsolidation_enabled=False,
        )
        normal_people = (
            storage == "postgres" and pipeline == "formation" and formation_policy == "formation@11"
        )
        if normal_people:
            from agent_core.adapters.models.registry import ADAPTER_DEFINITIONS
            from agent_core.config import PACKAGE_ROOT
            from agent_core.model.registry import ProviderRegistry, StaticModelRouter

            formation_profile = MemoryProfiles.from_document(
                load_config_document(effective, "memory/profiles.yaml")
            ).formation
            if resolved.provider != "fake":
                formation_model = await StaticModelRouter(
                    ProviderRegistry.load(
                        PACKAGE_ROOT / "models",
                        adapters=ADAPTER_DEFINITIONS,
                        overlay_root=effective.config_dir,
                    ),
                    fixed_clock(case.history[0].at),
                ).resolve(formation_profile.model_policy, tenant_id=principal.tenant_id)
                if formation_model.provider != resolved.provider:
                    raise ValueError("PostgreSQL formation must share the budgeted reader provider")
            # Exercise the normal, explicitly pinned People composition. Its older
            # evaluation-only mode deliberately remains restricted to memory storage.
            effective = replace(
                effective,
                memory_provider_extraction_mode=MemoryProviderExtractionMode.REQUIRED,
                memory_formation_policy_pin=MemoryFormationPolicyPin.PEOPLE,
            )
        bank = None
        if formation_bank_path is not None:
            identity = hashlib.sha256(
                json.dumps(
                    {
                        "case": case.model_dump(mode="json"),
                        "formation_policy": formation_policy,
                        "storage": storage,
                        "formation": load_config_document(effective, "memory/profiles.yaml")[
                            "formation"
                        ],
                        "implementation": implementation_digest(
                            Path(__file__).resolve().parents[3]
                        ),
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            bank = FormationBank(measured, formation_bank_path, identity)
        async with build(
            settings=effective,
            storage=storage,
            principal=principal,
            clock=EvaluationClock(case.history[0].at)
            if storage == "postgres"
            else fixed_clock(case.history[0].at),
            ids=fixture_ids(case.id),
            enabled_tools=["memory.search"],
            enabled_skills=[],
            script=FakeModelScript(turns=[ScriptedTurn(text="unused")])
            if pipeline == "seeded" and not production_reader
            else None,
            model_policy=resolved.policy_name,
            model_provider_overrides={resolved.provider: bank or measured},
            model_metadata_only=True,
            memory_distillation_evaluation_mode=pipeline == "formation"
            and formation_policy == "formation@9",
            memory_people_evaluation_mode=pipeline == "formation"
            and formation_policy == "formation@11"
            and not normal_people,
        ) as app:
            profile = app.memory_profiles.retrieval.model_copy(
                update={
                    "semantic_enabled": channel != "sparse",
                    "reranking": reranking,
                    "query_planning": query_planning,
                    "semantic_fusion_weight": semantic_fusion_weight,
                    "semantic_channel": "dense" if channel == "dense" else "hybrid",
                }
            )
            embedding = None
            if channel != "sparse":
                assert local is not None
                embedding = EmbeddingConfig(
                    space=local.space,
                    usd_per_million_tokens=Decimal(0),
                    pricing_revision="local-cpu@1",
                    egress_revision=LOCAL_EMBEDDING_REVISION,
                    residency_provider="local",
                    request_usd=Decimal("0.01"),
                    daily_usd=Decimal("0.10"),
                    historical_indexing=historical_indexing,
                )
            reader_runtime = reader_runtime_inputs(
                resolved,
                catalog=app.executor._model_settings if production_reader else None,
                router=app.executor._model_router if production_reader else None,
            )
            configuration = {
                "version": "recall-fixture-inputs@1",
                "composition": asdict(app.recall_composition),
                "retrieval": profile.model_dump(mode="json"),
                "embedding": None if embedding is None else embedding.model_dump(mode="json"),
                "local": None
                if channel == "sparse" or local is None
                else local.model_dump(mode="json"),
                "reader": resolved.model_dump(
                    mode="json", exclude={"resolved_at", "credential_ref"}
                ),
                "production_reader": production_reader,
                "reader_runtime": reader_runtime,
                "pipeline": pipeline,
                "formation_policy": formation_policy,
                "path": path,
                "channel": channel,
                "noise": noise,
                "execution": execution,
                "storage": storage,
                "source_recovery": recover_sources,
                "search_planning": search_planning,
            }
            configuration_sha256 = configuration_digest(configuration)
            confirmed_fields = {}
            if confirmation is not None:
                confirmation.validate(configuration)
                confirmed_fields = confirmation.observation_fields()
            if validate_composition is not None:
                validate_composition(configuration)
            if execution == "runtime":
                view = await app.services.sessions.create(
                    principal, "general", {"project_scope": case.task.scope}
                )
                await freeze_empty_task_context(app, view.id, resolved)
            sources, formation_runs = await _history(app, case, form=pipeline == "formation")
            if pipeline == "seeded":
                await _seed(app, case, sources)
            records = await app.memory.list_memories(include_inactive=True, limit=10000)
            if bank is not None:
                bank.finish(await _formed_state_digest(app, records))
            # This mapping is scoring-only and never reaches indexing or queries.
            labels = evidence_labels(case, records, sources)
            await _noise(app, case, noise)
            assert isinstance(app.clock, FixedClock)
            app.clock.advance(case.task.at - app.clock.now())
            if execution == "direct":
                view = await app.services.sessions.create(
                    principal, "general", {"project_scope": case.task.scope}
                )
            formation_ms = (perf_counter() - formation_started) * 1000
            lookup = None
            runtime_queries = None
            indexed = 0
            indexing_ms = 0.0
            if channel != "sparse":
                assert local is not None and embeddings is not None and embedding is not None
                worker = SemanticIndexingPass(
                    app.uow_factory,
                    app.clock,
                    principal,
                    embeddings,
                    embedding,
                    admitted=lambda: True,
                    local=local,
                )
                indexing_started = perf_counter()
                for _ in range(2 * ceil((len(records) + noise) / 16) + 2):
                    indexed_slice = await worker.run_slice()
                    indexed += indexed_slice.indexed
                    if indexed_slice.reason == "not_due":
                        break
                    if indexed_slice.reason not in {"indexed", "scanned"}:
                        raise ValueError(f"Incomplete index: {indexed_slice.reason}")
                else:
                    raise ValueError("Semantic inventory did not finish within its slice bound")
                indexing_ms = (perf_counter() - indexing_started) * 1000
                lookup = SemanticQueryPreparation(
                    app.uow_factory,
                    app.clock,
                    principal,
                    embeddings,
                    embedding,
                    admitted=lambda: True,
                    local=local,
                    context=lambda: SemanticQueryContext(
                        policy_version="m33-isolated@1",
                        retrieval_policy=f"{channel}@1",
                        surface_id="private",
                        task_sha256=hashlib.sha256(
                            case.task.model_dump_json().encode()
                        ).hexdigest(),
                        sensitivity_floor=Sensitivity.RESTRICTED,
                    ),
                )
                if storage == "postgres":
                    runtime_queries = run_bound_queries(
                        app,
                        embeddings,
                        embedding,
                        local,
                        profile,
                        resolved,
                        recover_sources=recover_sources,
                        production_reader=production_reader,
                    )
                    lookup = None
            retriever = _ObservedRetriever(
                app.uow_factory,
                app.clock,
                app.ids,
                principal,
                profile=profile,
                semantic_queries=lookup,
                runtime_semantic_queries=runtime_queries,
                reranking_admitted=lambda: True,
                source_recovery=BoundedSourceRecovery(principal, admitted=lambda: True)
                if recover_sources
                else None,
            )
            tool = MemorySearchTool(retriever)
            query = (
                DeterministicQueryFormer(principal, current_scope=case.task.scope).from_text(
                    case.task.question
                )[0]
                if path == "automatic"
                else tool.query(
                    {"text": case.task.question, "scope": case.task.scope},
                    tenant_id=principal.tenant_id,
                    principal_id=principal.principal_id,
                )
            )
            query = retriever._effective_query(query, "in_turn")
            started = perf_counter()
            if isinstance(app.clock, EvaluationClock):
                app.clock.start_live()
            runtime = None
            if execution == "runtime":
                runtime = await read_through_runtime(
                    app,
                    case.task,
                    view.id,
                    path,
                    retriever,
                    lookup,
                    measured,
                    resolved,
                    durable=storage == "postgres",
                    runtime_bound=runtime_queries is not None,
                    search_planning=search_planning,
                    production_reader=production_reader,
                )
                request, completed, answer = runtime.request, runtime.completed, runtime.answer
                if (
                    production_reader
                    and runtime.checkpoint.budget_state.get("context_reader_sha256")
                    != reader_runtime["reader_sha256"]
                ):
                    raise ValueError("Measured runtime reader identity changed")
                actual_results = visible_recall(retriever.results, request)
                # A context budget can drop the whole recall block. Never count
                # an earlier retriever result as rendered when the reader missed it.
                result = actual_results[0] if actual_results else retriever.observed
                if result is None:
                    raise ValueError("Runtime did not perform an observable recall")
                retrieval_ms = retriever.elapsed_ms
            else:
                if lookup is not None:
                    prepared = await lookup.prepare([query])
                    if prepared.prepared != 1 and prepared.reason != "cached":
                        raise ValueError("Sparse fallback cannot supply semantic observations")
                if path == "automatic":
                    result = await retriever.recall(query, session_id=view.id)
                else:
                    tool_result = await tool.execute(
                        {"text": case.task.question, "scope": case.task.scope},
                        replace(
                            tool_context(view.id),
                            tenant_id=principal.tenant_id,
                            principal=principal,
                        ),
                    )
                    if (
                        not tool_result.ok
                        or tool_result.structured is None
                        or retriever.observed is None
                    ):
                        raise ValueError("Deliberate retrieval did not produce an observed result")
                    result = retriever.observed
                retrieval_ms = (perf_counter() - started) * 1000
                assert result.stages is not None
                request = reader_request(
                    case.task, result.rendered, model_policy=resolved.policy_name
                )
                attempt = ModelAttempt(
                    attempt_id=app.ids.new_id(),
                    run_id=app.ids.new_id(),
                    step_number=1,
                    attempt_number=1,
                    started_at=app.clock.now(),
                )
                completed = None
                async with asyncio.timeout(30):
                    async for event in measured.stream(request, resolved, attempt):
                        if isinstance(event, ModelCompletedEvent):
                            if completed is not None or event.internal_retry_count:
                                raise ValueError("Evaluation reader retried or completed twice")
                            completed = event
                if (
                    completed is None
                    or completed.stop_reason != StopReason.END_TURN
                    or completed.turn.tool_calls
                ):
                    raise ValueError("Evaluation reader did not finish one ordinary response")
                answer = "\n".join(
                    part.text
                    for message in completed.turn.assistant_messages
                    for part in message.content
                    if part.kind == "text"
                )
                actual_results = (result,)
            assert result.stages is not None and completed is not None
            replayed_usages = bank.usages if bank is not None and bank.replaying else ()
            attributed_usages = [*replayed_usages, *measured.usages]
            score = score_answer(case, answer)

            def labeled(keys: tuple[UUID, ...] | list[UUID]) -> list[str]:
                return list(dict.fromkeys(label for key in keys for label in labels.get(key, ())))

            rendered_ids = list(
                dict.fromkeys(
                    item.belief_id
                    for observed in actual_results
                    for item in observed.items
                    if not item.blocked
                )
            )
            witnesses = recovered_source_witnesses(
                case, retriever.results, actual_results, sources, ranking=request.recall_ranking
            )
            global_order = observed_ranked_ids(retriever.results, ranking=request.recall_ranking)
            candidate_ids = tuple(
                dict.fromkeys(
                    key
                    for observed in retriever.results
                    if observed.stages is not None
                    for key in observed.stages.candidate_ids
                )
            )
            expected_sources = authored_source_evidence(case)
            recovered_labels = {
                stage: source_labels(expected_sources, witnesses, stage)
                for stage in ("candidates", "ranked", "rendered")
            }
            supported = set(labeled(rendered_ids)) | recovered_labels["rendered"]
            required = {label.id for label in case.evidence if label.required}
            useful_anchors = {
                witness.belief_id
                for witness in witnesses
                if witness.rendered
                and any(
                    label.required and witness.source_id in label.source_ids
                    for label in case.evidence
                )
            }
            async with app.uow_factory() as uow:
                traces = [
                    await uow.traces.get(observed.trace_id, principal)
                    for observed in retriever.results
                ]
                people_rows = await uow.people.query(
                    PeopleQuery(
                        tenant_id=principal.tenant_id,
                        principal_id=principal.principal_id,
                        sensitivity_ceiling=Sensitivity.RESTRICTED,
                        kinds=["person", "identifier", "relationship", "commitment", "memory_link"],
                        limit=100,
                    )
                )
            if confirmation is not None:
                confirmation.validate(configuration)
            return {
                **confirmed_fields,
                "observation_version": "general-recall-runtime-observation@1",
                "configuration_sha256": configuration_sha256,
                "reader_runtime": reader_runtime,
                "reader_identity_sha256": None
                if runtime is None
                else runtime.checkpoint.budget_state.get("context_reader_sha256"),
                "context_ranking": None
                if request.recall_ranking is None
                else request.recall_ranking.model_dump(mode="json"),
                "source_events": {
                    key: {"session_id": str(session), "sequence": sequence}
                    for key, (session, sequence) in sources.items()
                },
                "recall_observations": [
                    {
                        "query": trace.query.model_dump(mode="json"),
                        "policy": trace.retrieval_policy_version,
                        "rendered_in_request": observed in actual_results,
                        "result": observed.model_dump(mode="json"),
                        "stages": None
                        if observed.stages is None
                        else observed.stages.model_dump(mode="json"),
                        "candidate_ids": [str(key) for key in observed.stages.candidate_ids]
                        if observed.stages
                        else [],
                        "ranked_ids": [str(key) for key in observed.stages.ranked_ids]
                        if observed.stages
                        else [],
                        "rendered_ids": [str(item.belief_id) for item in observed.items],
                        "people": [item.model_dump(mode="json") for item in observed.people],
                        "source_candidates": [
                            source.model_dump(mode="json")
                            for source in observed.stages.source_candidates
                        ]
                        if observed.stages
                        else [],
                    }
                    for trace, observed in zip(traces, retriever.results, strict=True)
                ],
                "people_records": [row.model_dump(mode="json") for row in people_rows],
                "people_inventory_truncated": len(people_rows) > 100,
                "semantic_fallback": channel != "sparse"
                and not any("+semantic@" in trace.retrieval_policy_version for trace in traces),
                "execution": execution,
                "storage": storage,
                "semantic_binding": "run"
                if runtime_queries is not None
                else "isolated"
                if lookup is not None
                else "none",
                "clock": "elapsed" if storage == "postgres" else "fixed",
                "runtime_status": None if runtime is None else runtime.status,
                "tool_calls": 0 if runtime is None else runtime.tool_calls,
                "planning_calls": 0 if runtime is None else runtime.planning_calls,
                "recall_work": None
                if runtime is None or runtime.checkpoint.recall_work is None
                else runtime.checkpoint.recall_work.model_dump(mode="json"),
                "executed_searches": retriever.executed_searches,
                "activation_evidence": False,
                "scenario_id": case.id,
                "pipeline": pipeline,
                "formation_policy": formation_policy,
                "channel": channel,
                "path": path,
                "source_recovery": recover_sources,
                "semantic_fusion_weight": semantic_fusion_weight,
                "reranking": reranking,
                "query_planning": query_planning,
                "search_planning": search_planning,
                "formation_runs": formation_runs,
                "formation_bank_sha256": None if bank is None else bank.digest,
                "formation_replayed_calls": len(replayed_usages),
                "formed_records": [record.model_dump(mode="json") for record in records],
                "rendered_memory": "\n".join(observed.rendered for observed in actual_results),
                "provider_attempts": measured.attempts,
                "attributed_usage": [usage.model_dump(mode="json") for usage in attributed_usages],
                "formed": labeled(list(labels)),
                "candidates": labeled(candidate_ids),
                "global_rank_certifiable": global_order is not None,
                "ranked": labeled((global_order or ())[:20]),
                "rendered": labeled(rendered_ids),
                "recovered_sources": [witness.model_dump(mode="json") for witness in witnesses],
                "source_candidate_labels": sorted(recovered_labels["candidates"]),
                "source_ranked_labels": sorted(recovered_labels["ranked"]),
                "source_rendered_labels": sorted(recovered_labels["rendered"]),
                "rendered_noise_items": rendered_noise_items(
                    actual_results,
                    labels=labels,
                    required=required,
                    useful_anchors=useful_anchors,
                ),
                "answer_atoms": list(score.answer_atoms),
                "unsupported_atoms": list(score.unsupported_atoms),
                "reader_values_correct": score.correct,
                "answer_success": score.correct and required <= supported,
                "answer": answer,
                "invalid_current_ids": sorted(
                    set(answer_violations(case, answer))
                    | {
                        label.id
                        for label in case.evidence
                        if label.invalid_current and label.id in supported
                    }
                ),
                "context_tokens": sum(observed.tokens for observed in actual_results),
                "retrieval_ms": retrieval_ms,
                "formation_and_setup_ms": formation_ms,
                "indexing_ms": indexing_ms,
                "fixture_before_teardown_ms": (perf_counter() - formation_started) * 1000,
                "elapsed_ms": (perf_counter() - started) * 1000,
                "reader_usage": completed.turn.usage.model_dump(mode="json"),
                "provider_calls": measured.calls + len(replayed_usages),
                "actual_provider_calls": measured.calls,
                "input_tokens": sum(usage.input_tokens for usage in attributed_usages),
                "output_tokens": sum(usage.output_tokens for usage in attributed_usages),
                "cost_usd": str(sum((usage.cost for usage in attributed_usages), Decimal(0))),
                "actual_cost_usd": str(sum((usage.cost for usage in measured.usages), Decimal(0))),
                "reader_request_sha256": hashlib.sha256(
                    request.model_dump_json().encode()
                ).hexdigest(),
                "indexed": indexed,
                "retrieved_ids": [str(key) for key in rendered_ids],
            }
