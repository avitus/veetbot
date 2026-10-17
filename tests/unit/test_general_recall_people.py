"""The measured runtime must keep People recall on its admitted semantic path."""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import NAMESPACE_URL, UUID, uuid5

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
from agent_core.domain.people import Person, PersonMemoryLink
from agent_core.evals import general_recall_execution as execution
from agent_core.evals.general_recall_corpus import EvidenceFixture
from agent_core.evals.general_recall_observation import compile_runtime_observation
from agent_core.evals.people_execution import BudgetedProvider, EvaluationBudget
from tests.contract.test_local_semantic_preparation import LOCAL
from tests.gates.test_runtime_tool_budget import _settings
from tests.unit.test_general_recall_corpus import AT, fixture


@pytest.mark.parametrize("path", ["automatic", "deliberate"])
@pytest.mark.parametrize("channel", ["dense", "hybrid"])
async def test_measured_people_recall_keeps_semantics_through_reader_and_compiler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path: PathKind,
    channel: Channel,
    *,
    settings: Settings | None = None,
    storage: Literal["memory", "postgres"] = "memory",
) -> None:
    case = fixture().model_copy(
        update={
            "history": (
                fixture()
                .history[0]
                .model_copy(update={"content": "Alex requires step-free routes."}),
            ),
            "task": fixture().task.model_copy(
                update={"question": "Plan a step-free route with Alex."}
            ),
            "evidence": (
                EvidenceFixture(
                    id="accessibility",
                    source_ids=("h1",),
                    subject="Alex",
                    statement="Alex requires step-free routes",
                ),
            ),
        }
    )
    original_seed = execution._seed

    async def seed_people(app: Any, actual: Any, sources: dict[str, tuple[UUID, int]]) -> None:
        await original_seed(app, actual, sources)
        fields = {
            "tenant_id": app.principal.tenant_id,
            "principal_id": app.principal.principal_id,
            "created_at": app.clock.now(),
            "updated_at": app.clock.now(),
        }
        person = Person(id=UUID(int=9100), display_name="Alex", **fields)
        async with app.uow_factory() as uow:
            await uow.people.put(person, expected_revision=0)
            await uow.people.put(
                PersonMemoryLink(
                    id=UUID(int=9101),
                    person_id=person.id,
                    belief_id=uuid5(NAMESPACE_URL, f"m33:{actual.id}:accessibility"),
                    **fields,
                ),
                expected_revision=0,
            )

    monkeypatch.setattr(execution, "_seed", seed_people)

    class Embeddings:
        async def embed(self, request: EmbeddingRequest) -> EmbeddingResponse:
            return EmbeddingResponse(
                request_digest=request.digest,
                vectors=tuple((1.0, 0.0, 0.0) for _ in request.texts),
                input_tokens=2,
            )

    planned = (
        [
            ScriptedTurn(
                tool_calls=[
                    ScriptedToolCall(
                        name="memory.search",
                        call_id="planned",
                        arguments={"text": case.task.question, "scope": "general"},
                    )
                ],
                stop_reason=StopReason.TOOL_USE,
            )
        ]
        if path == "deliberate"
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
    row = await execution.evaluate_fixture(
        case=case,
        settings=settings or _settings(),
        pipeline="seeded",
        channel=channel,
        path=path,
        reader=provider,
        resolved=ResolvedModel(provider="fake", model="scripted", resolved_at=AT),
        noise=0,
        storage=storage,
        disposable_database=storage == "postgres",
        search_planning="reader",
        local=LOCAL,
        embeddings=Embeddings(),
    )
    assert any(search["result"]["people"] for search in row["recall_observations"])
    assert "Alex requires step-free routes" in inner.requests[-1].model_dump_json()
    assert row["semantic_fallback"] is False
    if storage == "postgres":
        compiled = compile_runtime_observation(
            case,
            {**row, "observed_at": datetime.now(UTC).isoformat()},
            identity_sha256="a" * 64,
        )
        assert compiled.rendered == ("accessibility",)
        assert compiled.boundary_failures == ()
    assert provider.budget.calls == 1 + int(path == "deliberate")
