"""Production-shaped synthetic recall uses the actual PostgreSQL worker path."""

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, uuid5

import pytest

from agent_core.adapters.determinism import FixedClock
from agent_core.adapters.models.fake import FakeModelProvider
from agent_core.config import Settings
from agent_core.domain.embeddings import EmbeddingRequest, EmbeddingResponse
from agent_core.domain.general_recall_release import Channel, PathKind
from agent_core.domain.messages import (
    FakeModelScript,
    ResolvedModel,
    ScriptedToolCall,
    ScriptedTurn,
    StopReason,
)
from agent_core.domain.runs import Run, RunCheckpoint
from agent_core.domain.sessions import Session
from agent_core.evals.general_recall_corpus import EvidenceFixture
from agent_core.evals.general_recall_execution import evaluate_fixture
from agent_core.evals.people_execution import BudgetedProvider, EvaluationBudget
from agent_core.memory.semantic_runtime import SemanticQueryAuthority
from tests.integration.disposable_database import clone_recall_database, paired_recall_databases
from tests.integration.m2_support import database_settings
from tests.unit.test_general_recall_corpus import AT, fixture


@pytest.fixture
async def recall_database() -> AsyncIterator[Settings]:
    # The integration autouse fixture has already emptied the explicitly marked
    # test database. Clone its migrated schema into a uniquely named scratch DB.
    settings = database_settings()
    async with clone_recall_database() as database_url:
        yield replace(settings, database_url=database_url)


@pytest.mark.parametrize("channel", ["sparse", "dense", "hybrid"])
async def test_deliberate_recall_budget_starts_after_slow_reader_planning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel: Channel,
    recall_database: Settings,
) -> None:
    from tests.unit.test_general_recall_execution import (
        test_deliberate_measurement_reserves_only_after_reader_plans_search as exercise,
    )

    await exercise(tmp_path, monkeypatch, channel, settings=recall_database, storage="postgres")


@pytest.mark.parametrize("path", ["automatic", "deliberate"])
@pytest.mark.parametrize("channel", ["sparse", "hybrid"])
@pytest.mark.parametrize("search_planning", ["scripted", "reader"])
async def test_observation_runs_in_postgres_and_reaches_the_reader(
    tmp_path: Path,
    path: PathKind,
    channel: Channel,
    search_planning: Literal["scripted", "reader"],
    recall_database: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.adapters.persistence.unit_of_work import _unit_of_work_is_open
    from agent_core.evals.general_memory_recall_driver import OWNER
    from agent_core.memory.local_query_authority import LocalQueryAuthority
    from tests.contract.test_local_semantic_preparation import LOCAL

    owner = OWNER.model_copy(
        update={"tenant_id": "reviewed-tenant", "principal_id": "reviewed-owner"}
    )
    admitted: list[bool] = []
    original = LocalQueryAuthority.__call__

    async def observed_authority(
        self: LocalQueryAuthority, run: Run, session: Session, checkpoint: RunCheckpoint
    ) -> SemanticQueryAuthority | None:
        result = await original(self, run, session, checkpoint)
        admitted.append(result is not None)
        return result

    monkeypatch.setattr(LocalQueryAuthority, "__call__", observed_authority)

    class Embeddings:
        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            assert not _unit_of_work_is_open()
            return EmbeddingResponse(
                request_digest=request.digest,
                vectors=tuple(
                    (1.0, 0.0, 0.0) if "step-free" in value else (0.0, 1.0, 0.0)
                    for value in request.texts
                ),
                input_tokens=2,
            )

    case = fixture().model_copy(
        update={
            "evidence": (
                EvidenceFixture(
                    id="accessibility",
                    source_ids=("h1",),
                    subject="routes",
                    statement="User requires step-free routes",
                ),
            )
        }
    )
    planned = (
        [
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.search",
                        call_id="planned",
                        arguments={"text": "step-free routes", "scope": "general"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
        if search_planning == "reader"
        else []
    )
    inner = FakeModelProvider(
        FakeModelScript(
            turns=[
                *planned,
                ScriptedTurn(
                    text=json.dumps({"action": "answer", "values": {"route": "east ramp"}})
                ),
            ]
        ),
        FixedClock(AT),
    )
    provider = BudgetedProvider(inner, EvaluationBudget(Decimal("0.5"), tmp_path / "budget.jsonl"))
    row = await evaluate_fixture(
        case=case,
        settings=recall_database,
        principal=owner,
        pipeline="seeded",
        channel=channel,
        path=path,
        reader=provider,
        resolved=ResolvedModel(provider="fake", model="scripted", resolved_at=AT),
        noise=10,
        storage="postgres",
        disposable_database=True,
        search_planning=search_planning,
        local=LOCAL if channel != "sparse" else None,
        embeddings=Embeddings() if channel != "sparse" else None,
    )
    from agent_core.bootstrap import build

    async with (
        build(settings=recall_database, storage="postgres", principal=owner) as app,
        app.uow_factory() as uow,
    ):
        record = await uow.memories.get(uuid5(NAMESPACE_URL, f"m33:{case.id}:accessibility"), owner)
    assert record is not None, "The experiment must use the real PostgreSQL repository"
    assert row["storage"] == "postgres" and row["runtime_status"] == "COMPLETED"
    assert row["answer_success"] and row["rendered"] == ["accessibility"]
    searched = path == "deliberate" or search_planning == "reader"
    assert row["tool_calls"] == int(searched)
    assert row["planning_calls"] == int(search_planning == "reader")
    automatic_slots = 2 if path == "automatic" else 0
    assert row["recall_work"]["searches"] == automatic_slots + int(searched)
    assert row["recall_work"]["rounds"] == int(path == "automatic") + int(searched)
    assert 1 <= row["executed_searches"] <= row["recall_work"]["searches"] <= 4
    from datetime import UTC, datetime

    from agent_core.evals.general_recall_observation import compile_runtime_observation

    compiled = compile_runtime_observation(
        case,
        {**row, "observed_at": datetime.now(UTC).isoformat()},
        identity_sha256="a" * 64,
        principal=owner,
    )
    assert compiled.rendered == ("accessibility",)
    assert compiled.answer_atoms == ("route",)
    assert compiled.boundary_failures == ()
    if channel != "sparse":
        assert any(admitted), "The measured semantic path must validate actual run/reader authority"
        assert row["semantic_fallback"] is False


async def test_a_second_experiment_cannot_reset_a_running_case(recall_database: Settings) -> None:
    from agent_core.evals.general_recall_database import evaluation_database

    async with evaluation_database(recall_database, storage="postgres", authorized=True):
        with pytest.raises(ValueError, match="active experiment"):
            async with evaluation_database(recall_database, storage="postgres", authorized=True):
                pytest.fail("The second experiment must not enter the database")
    async with evaluation_database(recall_database, storage="postgres", authorized=True):
        pass  # Completion releases the lock for the next case.


async def test_parallel_cases_isolate_the_same_owner_and_share_one_provider_budget(
    tmp_path: Path,
) -> None:
    import asyncio

    from agent_core.domain.messages import ModelAttempt, ModelEvent, ModelRequest
    from tests.contract.test_local_semantic_preparation import LOCAL

    reached_reader: set[str] = set()
    ready = asyncio.Event()
    shared = EvaluationBudget(Decimal("1"), tmp_path / "shared.jsonl")

    class Reader(FakeModelProvider):
        def __init__(self, label: str) -> None:
            super().__init__(
                FakeModelScript(
                    turns=[ScriptedTurn(text='{"action":"clarify","values":{"route":null}}')]
                ),
                FixedClock(AT),
            )
            self.label = label

        async def stream(
            self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
        ) -> AsyncIterator[ModelEvent]:
            reached_reader.add(self.label)
            if len(reached_reader) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=5)
            async for event in super().stream(request, resolved, attempt):
                yield event

    class Embeddings:
        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            return EmbeddingResponse(
                request_digest=request.digest,
                vectors=tuple((1.0, 0.0, 0.0) for _ in request.texts),
                input_tokens=len(request.texts),
            )

    async def measure(database_url: str, label: str) -> dict[str, Any]:
        base = fixture()
        statement = f"User requires {label} routes."
        case = base.model_copy(
            update={
                "id": f"isolated-{label}",
                "history": (base.history[0].model_copy(update={"content": statement}),),
                "task": base.task.model_copy(
                    update={"question": f"What {label} routes do I require?"}
                ),
                "evidence": (
                    EvidenceFixture(
                        id="accessibility",
                        source_ids=(base.history[0].id,),
                        subject="routes",
                        statement=statement,
                    ),
                ),
            }
        )
        result = await evaluate_fixture(
            case=case,
            settings=replace(database_settings(), database_url=database_url),
            pipeline="seeded",
            channel="hybrid",
            path="automatic",
            reader=BudgetedProvider(Reader(label), shared),
            resolved=ResolvedModel(provider="fake", model="scripted", resolved_at=AT),
            noise=0,
            storage="postgres",
            disposable_database=True,
            local=LOCAL,
            embeddings=Embeddings(),
            historical_indexing=True,
        )
        assert result["rendered"] == ["accessibility"]
        assert [record["statement"] for record in result["formed_records"]] == [statement]
        assert result["semantic_fallback"] is False
        return result

    async with paired_recall_databases() as (first, second):
        rows = await asyncio.gather(measure(first, "step-free"), measure(second, "weatherproof"))
    assert reached_reader == {"step-free", "weatherproof"}
    assert len({row["scenario_id"] for row in rows}) == 2
    assert set(rows[0]["retrieved_ids"]).isdisjoint(rows[1]["retrieved_ids"])
    assert shared.calls == 2 and shared.spent == shared.held == 0


async def test_confirmation_is_checked_before_provider_work_and_preserved_in_actual_row(
    tmp_path: Path, recall_database: Settings
) -> None:
    from agent_core.evals.general_memory_recall_driver import OWNER
    from agent_core.evals.general_recall_confirmation import capture_runtime_configurations
    from agent_core.evals.general_recall_experiment import Experiment
    from tests.contract.test_local_semantic_preparation import LOCAL
    from tests.unit.test_general_recall_confirmation import binding

    inner = FakeModelProvider(
        FakeModelScript(
            turns=[ScriptedTurn(text='{"action":"answer","values":{"route":"east ramp"}}')]
        ),
        FixedClock(AT),
    )
    provider = BudgetedProvider(inner, EvaluationBudget(Decimal("0.5"), tmp_path / "budget.jsonl"))
    arguments: dict[str, Any] = {
        "production_reader": True,
        "case": fixture(),
        "settings": recall_database,
        "pipeline": "seeded",
        "channel": "sparse",
        "path": "automatic",
        "reader": provider,
        "resolved": ResolvedModel(
            provider="fake", model="scripted", policy_name="fake-balanced", resolved_at=AT
        ),
        "noise": 0,
        "storage": "postgres",
        "disposable_database": True,
    }
    captured = await capture_runtime_configurations(
        Experiment(
            corpus=tmp_path / "unopened.jsonl",
            output=tmp_path / "unused",
            noise=0,
            storage="postgres",
            disposable_database=True,
            production_reader=True,
        ),
        settings=recall_database,
        principal=OWNER,
        resolved=arguments["resolved"],
        local=LOCAL,
        maximum_cost_usd=Decimal("0.5"),
    )
    assert len(captured.variants) == 12 and not (tmp_path / "unopened.jsonl").exists()
    assert not inner.requests and provider.budget.calls == 0
    # The matrix was captured from real scratch compositions. The fabricated
    # release identity and scripted reader supply no release quality evidence.
    confirmed = binding(captured)
    row = await evaluate_fixture(**arguments, confirmation=confirmed)
    confirmed.validate_observation(row)
    assert row["identity_sha256"] == confirmed.observation_fields()["identity_sha256"]
    assert provider.budget.calls > 0
    before = provider.budget.calls
    arguments["noise"] = 1
    with pytest.raises(ValueError, match="runtime configuration changed"):
        await evaluate_fixture(**arguments, confirmation=confirmed)
    assert provider.budget.calls == before


async def test_temporal_seed_preserves_foreign_keys_and_withholds_superseded_values(
    tmp_path: Path, recall_database: Settings
) -> None:
    from agent_core.evals.general_recall_corpus import load_corpus

    case = next(
        case
        for case in load_corpus(Path("evals/capability/general-recall/pilot.v1.jsonl"))
        if case.id == "meeting-time-correction"
    )
    inner = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text=json.dumps(
                        {
                            "action": "answer",
                            "values": {
                                key: values[0] for key, values in case.expected_values.items()
                            },
                        }
                    )
                )
            ]
        ),
        FixedClock(AT),
    )
    provider = BudgetedProvider(inner, EvaluationBudget(Decimal("0.5"), tmp_path / "budget.jsonl"))
    row = await evaluate_fixture(
        case=case,
        settings=recall_database,
        pipeline="seeded",
        channel="sparse",
        path="automatic",
        reader=provider,
        resolved=ResolvedModel(provider="fake", model="scripted", resolved_at=AT),
        noise=0,
        storage="postgres",
        disposable_database=True,
    )
    assert row["answer_success"] and row["invalid_current_ids"] == []
    assert {label.id for label in case.evidence if label.invalid_current}.isdisjoint(
        row["rendered"]
    )


async def test_formation_uses_the_normal_people_path_in_guarded_postgres(
    tmp_path: Path, recall_database: Settings
) -> None:
    inner = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(text=json.dumps({"action": "clarify", "values": {"route": None}}))
                for _ in range(8)
            ]
        ),
        FixedClock(AT),
    )
    provider = BudgetedProvider(inner, EvaluationBudget(Decimal("0.5"), tmp_path / "budget.jsonl"))
    row = await evaluate_fixture(
        case=fixture(),
        settings=recall_database,
        pipeline="formation",
        channel="sparse",
        path="automatic",
        reader=provider,
        resolved=ResolvedModel(
            provider="fake", model="scripted", policy_name="fake-balanced", resolved_at=AT
        ),
        noise=0,
        storage="postgres",
        disposable_database=True,
    )
    assert row["formation_runs"][0]["run"]["policy_version"] == "formation@11"
    assert row["provider_attempts"] and row["runtime_status"] == "COMPLETED"


async def test_postgres_paths_replay_the_same_formation_before_reading(
    tmp_path: Path, recall_database: Settings
) -> None:
    observed = []
    paths: tuple[PathKind, ...] = ("automatic", "deliberate")
    for index, path in enumerate(paths):
        provider = BudgetedProvider(
            FakeModelProvider(
                FakeModelScript(
                    turns=[
                        ScriptedTurn(
                            text=json.dumps({"action": "clarify", "values": {"route": None}})
                        )
                        for _ in range(8)
                    ]
                ),
                FixedClock(AT),
            ),
            EvaluationBudget(Decimal("0.5"), tmp_path / f"budget-{index}.jsonl"),
        )
        row = await evaluate_fixture(
            case=fixture(),
            settings=recall_database,
            pipeline="formation",
            channel="sparse",
            path=path,
            reader=provider,
            resolved=ResolvedModel(
                provider="fake", model="scripted", policy_name="fake-balanced", resolved_at=AT
            ),
            noise=0,
            storage="postgres",
            disposable_database=True,
            formation_bank_path=tmp_path / "formation.json",
        )
        observed.append(row)
        assert row["actual_provider_calls"] == provider.budget.calls
        if index:
            assert provider.budget.calls == 1 and row["formation_replayed_calls"] > 0
    assert observed[0]["formation_bank_sha256"] == observed[1]["formation_bank_sha256"]


async def test_dated_semantic_tool_result_survives_actual_reader_context(
    tmp_path: Path,
    recall_database: Settings,
) -> None:
    from datetime import timedelta

    from agent_core.domain.memory import MemoryStatus
    from agent_core.evals.general_recall_corpus import HistoryTurn
    from tests.contract.test_local_semantic_preparation import LOCAL

    case = fixture()
    changed = AT - timedelta(days=1)
    historical_at = AT - timedelta(days=7)
    case = case.model_copy(
        update={
            "history": (
                *case.history,
                HistoryTurn(id="h2", session="s2", at=changed, content="I can use stairs again."),
            ),
            "task": case.task.model_copy(
                update={
                    "question": (
                        "Which pickup route fitted my mobility seven days ago: "
                        "the stairs or the east ramp?"
                    )
                }
            ),
            "evidence": (
                EvidenceFixture(
                    id="previous-mobility",
                    source_ids=("h1",),
                    subject="routes",
                    statement="User requires step-free routes",
                    status=MemoryStatus.SUPERSEDED,
                    valid_to=changed,
                    superseded_by="current-mobility",
                ),
                EvidenceFixture(
                    id="current-mobility",
                    source_ids=("h2",),
                    subject="routes",
                    statement="User can now use stairs",
                    required=False,
                ),
            ),
        }
    )

    class Embeddings:
        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            return EmbeddingResponse(
                request_digest=request.digest,
                vectors=tuple((1.0, 0.0, 0.0) for _ in request.texts),
                input_tokens=2,
            )

    inner = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    tool_calls=[
                        ScriptedToolCall(
                            name="memory.search",
                            call_id="dated-search",
                            arguments={
                                "text": "previous mobility routes",
                                "scope": "general",
                                "as_of": historical_at.isoformat(),
                            },
                        )
                    ],
                    stop_reason=StopReason.TOOL_USE,
                ),
                ScriptedTurn(
                    text=json.dumps({"action": "answer", "values": {"route": "east ramp"}})
                ),
            ]
        ),
        FixedClock(AT),
    )
    provider = BudgetedProvider(inner, EvaluationBudget(Decimal("0.5"), tmp_path / "budget.jsonl"))
    row = await evaluate_fixture(
        case=case,
        settings=recall_database,
        pipeline="seeded",
        channel="dense",
        path="deliberate",
        reader=provider,
        resolved=ResolvedModel(provider="fake", model="scripted", resolved_at=AT),
        noise=10,
        storage="postgres",
        disposable_database=True,
        search_planning="reader",
        local=LOCAL,
        embeddings=Embeddings(),
        historical_indexing=True,
    )
    assert row["runtime_status"] == "COMPLETED" and row["answer_success"]
    assert "previous-mobility" in row["rendered"]
    assert row["tool_calls"] == 1 and row["planning_calls"] == 1
    assert row["executed_searches"] <= 4
    final_request = inner.requests[-1]
    encoded = final_request.model_dump_json()
    assert "User requires step-free routes" in encoded
    assert historical_at.isoformat().replace("+00:00", "Z") in encoded
