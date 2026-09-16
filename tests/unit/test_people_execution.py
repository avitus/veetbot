"""Comparison costs survive failed calls and cannot bypass the requested cap."""

import json
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agent_core.domain.errors import BudgetExceededError
from agent_core.domain.messages import (
    ModelAttempt,
    ModelCompletedEvent,
    ModelEvent,
    ModelLimits,
    ModelPricing,
    ModelRequest,
    ModelTurn,
    ModelUsage,
    ResolvedModel,
    StopReason,
)
from agent_core.evals.people_execution import BudgetedProvider, EvaluationBudget
from tests.contract.support import NOW, ids


async def test_task_answers_can_only_cite_evidence_actually_injected(tmp_path: Path) -> None:
    from agent_core.adapters.determinism import FixedClock
    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.domain.memory import RecallResult
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn, TextPart, UserMessage
    from agent_core.domain.policies import TrustLevel
    from agent_core.evals.people_comparison import answer_task

    clock = FixedClock(NOW)
    fake = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(
                    text=json.dumps(
                        {
                            "answer": "Maya enjoys chess",
                            "citations": ["m:12345678", "invented-reference"],
                        }
                    )
                )
            ]
        ),
        clock,
    )
    provider = BudgetedProvider(fake, EvaluationBudget(Decimal("1"), tmp_path / "calls.jsonl"))
    result = RecallResult(
        items=[],
        people=[],
        rendered="[m:12345678] Maya enjoys chess.",
        tokens=20,
        truncated=False,
        trace_id=uuid4(),
    )
    observed = await answer_task(
        provider,
        ResolvedModel(provider="fake", model="scripted", resolved_at=NOW),
        question="What does Maya enjoy?",
        result=result,
        citations={"m:12345678": [4], "another": [7]},
        clock=clock,
        ids=ids(),
        index=0,
    )
    assert observed.answer == "Maya enjoys chess"
    assert observed.evidence_events == [4]
    assert observed.unsupported_citations == 1
    assert observed.retrieved_evidence_events == [4, 7]
    injected = fake.requests[0].conversation[1]
    assert isinstance(injected, UserMessage) and injected.trust == TrustLevel.MEMORY
    assert fake.requests[0].tools == []
    question = fake.requests[0].conversation[-1]
    assert isinstance(question, UserMessage)
    assert question.content == [TextPart(text="What does Maya enjoy?")]


@pytest.mark.parametrize("retrospective", [False, True])
async def test_comparison_replays_real_formation_without_gold_or_future_source_leak(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retrospective: bool,
) -> None:
    from agent_core.adapters.determinism import FixedClock
    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.application.people_context import PeopleContextService
    from agent_core.domain.agents import Principal
    from agent_core.domain.memory import RecallQuery, RecallResult
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from agent_core.evals.people import PeopleCase
    from agent_core.evals.people_comparison import evaluate_case
    from tests.integration.m2_support import memory_settings

    recall = PeopleContextService.automatic_recall

    async def checked_recall(
        self: PeopleContextService, owner: Principal, query: RecallQuery, **kwargs: Any
    ) -> RecallResult:
        assert query.min_score == 0.12, "comparison must use the deployed recall threshold"
        assert query.budget_tokens == 2000 and query.max_items == 20
        if retrospective:
            from datetime import UTC, datetime

            assert query.as_of == datetime(2025, 1, 1, tzinfo=UTC)
            assert query.known_at == datetime(2025, 1, 2, tzinfo=UTC)
        return await recall(self, owner, query, **kwargs)

    monkeypatch.setattr(PeopleContextService, "automatic_recall", checked_recall)

    case = PeopleCase.model_validate(
        {
            "id": "synthetic",
            "family": "test",
            "collision": False,
            "events": [
                {
                    "session": "first",
                    "occurred_at": "2025-01-01T00:00:00Z",
                    "text": "Maya likes chess.",
                },
                {
                    "session": "second",
                    "occurred_at": "2025-01-02T00:00:00Z",
                    "text": "AFTER_SOURCE Maya likes pottery.",
                },
            ],
            "mentions": [
                {
                    "event": 0,
                    "start": 0,
                    "end": 4,
                    "text": "Maya",
                    "entity": "gold-identity-only",
                    "resolvable": True,
                }
            ],
            "facts": [],
            "tasks": [
                {
                    "question": "What does Maya like?",
                    "entity": "gold-identity-only",
                    "evidence_events": [0],
                    "terms": ["gold-answer-only"],
                    "as_of": "2025-01-01T00:00:00Z" if retrospective else None,
                    "known_at": "2025-01-02T00:00:00Z" if retrospective else None,
                }
            ],
        }
    )
    turns = ['{"episodes":[]}', '{"predictions":[]}', '{"candidates":[],"coverage":[]}'] * 2
    turns.append('{"answer":"I do not know","citations":[]}')
    fake = FakeModelProvider(
        FakeModelScript(turns=[ScriptedTurn(text=t) for t in turns]), FixedClock(NOW)
    )
    provider = BudgetedProvider(fake, EvaluationBudget(Decimal("1"), tmp_path / "calls.jsonl"))
    observed = await evaluate_case(
        memory_settings(),
        case,
        pipeline="full-people",
        repeat=0,
        model_policy="deterministic",
        policy_profile="default",
        provider=provider,
        resolved=ResolvedModel(provider="fake", model="scripted", resolved_at=NOW),
    )
    assert observed.provider_calls == 7 and observed.tasks[0].answer == "I do not know"
    assert observed.formation_calls == 6 and observed.formation_segments == 2
    assert observed.tasks[0].retrieved_evidence_events == []
    requests = [request.model_dump_json() for request in fake.requests]
    assert all(
        "gold-identity-only" not in value and "gold-answer-only" not in value for value in requests
    )
    assert all("AFTER_SOURCE" not in value for value in requests[:3])
    assert any("AFTER_SOURCE" in value for value in requests[3:6])


class CompletingProvider:
    name = "test"

    def __init__(self, cost: Decimal | None) -> None:
        self.cost = cost
        self.calls = 0

    async def close(self) -> None:
        pass

    async def stream(
        self, request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        self.calls += 1
        if self.cost is None:
            raise RuntimeError("synthetic interrupted transport")
        yield ModelCompletedEvent(
            attempt_id=attempt.attempt_id,
            run_id=attempt.run_id,
            step_number=1,
            sequence=1,
            stop_reason=StopReason.END_TURN,
            turn=ModelTurn(stop_reason=StopReason.END_TURN, usage=ModelUsage(cost=self.cost)),
        )


def request_model_attempt() -> tuple[ModelRequest, ResolvedModel, ModelAttempt]:
    return (
        ModelRequest(model_policy="test", conversation=[], tools=[], maximum_output_tokens=100),
        ResolvedModel(
            provider="test",
            model="test",
            resolved_at=NOW,
            limits=ModelLimits(
                context_window_tokens=1000, max_output_tokens=100, default_output_reserve=100
            ),
            pricing=ModelPricing(input_per_mtok=Decimal(100), output_per_mtok=Decimal(100)),
        ),
        ModelAttempt(
            attempt_id=uuid4(), run_id=uuid4(), step_number=1, attempt_number=1, started_at=NOW
        ),
    )


@pytest.mark.parametrize("cost", [Decimal("0.03"), None, Decimal("0.12")])
async def test_budget_reserves_before_egress_and_keeps_unknown_charges(
    tmp_path: Path, cost: Decimal | None
) -> None:
    journal = tmp_path / "calls.jsonl"
    budget = EvaluationBudget(Decimal("0.12"), journal)
    inner = CompletingProvider(cost)
    provider = BudgetedProvider(inner, budget)
    args = request_model_attempt()
    if cost is None:
        with pytest.raises(RuntimeError):
            _ = [event async for event in provider.stream(*args)]
        assert budget.held == Decimal("0.11") and budget.spent == 0
    elif cost == Decimal("0.12"):
        with pytest.raises(BudgetExceededError):
            _ = [event async for event in provider.stream(*args)]
        assert budget.spent == cost and budget.held == 0
    else:
        _ = [event async for event in provider.stream(*args)]
        assert budget.spent == cost and budget.held == 0
    assert inner.calls == 1
    with pytest.raises(BudgetExceededError):
        _ = [event async for event in provider.stream(*args)]
    assert inner.calls == 1
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    assert rows[0]["outcome"] == "reserved"
    assert rows[0]["reservation_usd"] == "0.11"
    assert rows[-1]["outcome"] == (
        "uncertain" if cost is None else "overrun" if cost > Decimal("0.11") else "completed"
    )
    assert "conversation" not in str(rows) and "synthetic interrupted" not in str(rows)
    with pytest.raises(FileExistsError):
        EvaluationBudget(Decimal("100"), journal)


def test_comparison_rejects_untracked_executable_inputs(tmp_path: Path) -> None:
    import subprocess

    from agent_core.evals.memory_distillation import require_committed_tree

    def git(*args: str) -> str:
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-q")
    git(
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "--allow-empty",
        "-qm",
        "fixture",
    )
    head = git("rev-parse", "HEAD")
    require_committed_tree(tmp_path, head)
    (tmp_path / "new_extractor.py").write_text("def extract(): return []\n")
    with pytest.raises(ValueError, match="uncommitted"):
        require_committed_tree(tmp_path, head)


async def test_ordinary_benchmark_evaluates_people_policy_through_same_scoring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any

    from agent_core import bootstrap
    from agent_core.adapters.determinism import FixedClock
    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from agent_core.evals.memory_distillation import _evaluate_case, load_distillation_corpus
    from tests.integration.m2_support import memory_settings

    original = bootstrap.build
    fake = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(text=text)
                for text in [
                    '{"episodes":[]}',
                    '{"predictions":[]}',
                    '{"candidates":[],"coverage":[],"interactions":[]}',
                ]
            ]
        ),
        FixedClock(NOW),
    )

    def build_with_fake(**kwargs: Any) -> Any:
        return original(**{**kwargs, "model_provider_overrides": {"fake": fake}})

    monkeypatch.setattr(bootstrap, "build", build_with_fake)
    corpus, _ = load_distillation_corpus(Path.cwd())
    case = next(
        case for case in corpus.cases if len(case.events) == 1 and case.events[0].actor == "user"
    )
    result = await _evaluate_case(
        memory_settings(),
        case,
        model_policy="deterministic",
        policy_profile="default",
        policy_version="formation@11",
    )
    assert result.provider_calls == result.expected_provider_calls == 3
    assert result.score.scoring == "strict"
    assert result.policy_version == "formation@11"


async def test_ordinary_comparison_keeps_transport_open_and_journals_every_arm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_core.adapters.determinism import FixedClock
    from agent_core.adapters.models.fake import FakeModelProvider
    from agent_core.domain.messages import FakeModelScript, ScriptedTurn
    from agent_core.evals import people_ordinary
    from agent_core.evals.memory_distillation import (
        load_distillation_corpus,
        load_distillation_holdout,
    )
    from tests.integration.m2_support import memory_settings

    development, digest = load_distillation_corpus(Path.cwd())
    holdout, holdout_digest = load_distillation_holdout(Path.cwd())
    case = next(
        case
        for case in development.cases
        if len(case.events) == 1
        and case.events[0].actor == "user"
        and case.prior_beliefs_pool is None
    )
    monkeypatch.setattr(
        people_ordinary,
        "load_distillation_corpus",
        lambda _root: (development.model_copy(update={"cases": [case]}), digest),
    )
    monkeypatch.setattr(
        people_ordinary,
        "load_distillation_holdout",
        lambda _root: (
            holdout.model_copy(update={"cases": [case.model_copy(update={"id": "held"})]}),
            holdout_digest,
        ),
    )
    fake = FakeModelProvider(
        FakeModelScript(
            turns=[
                ScriptedTurn(text=text)
                for text in [
                    '{"episodes":[]}',
                    '{"predictions":[]}',
                    '{"candidates":[],"coverage":[],"interactions":[]}',
                ]
                * 12
            ]
        ),
        FixedClock(NOW),
    )
    closed = False
    original_stream = fake.stream

    async def close() -> None:
        nonlocal closed
        closed = True

    async def stream(
        request: ModelRequest, resolved: ResolvedModel, attempt: ModelAttempt
    ) -> AsyncIterator[ModelEvent]:
        assert not closed, "one case closed the transport needed by following cases"
        async for event in original_stream(request, resolved, attempt):
            yield event

    monkeypatch.setattr(fake, "close", close)
    monkeypatch.setattr(fake, "stream", stream)
    provider = BudgetedProvider(fake, EvaluationBudget(Decimal("1"), tmp_path / "budget.jsonl"))
    report = await people_ordinary.run_ordinary_comparison(
        Path.cwd(),
        output=tmp_path,
        settings=memory_settings(),
        model_policy="deterministic",
        policy_profile="default",
        provider=provider,
        resolved=ResolvedModel(provider="fake", model="scripted", resolved_at=NOW),
        repeats=3,
    )
    assert report["corpus_sha256"] == digest and report["holdout_sha256"] == holdout_digest
    summaries = report["reports"]
    assert isinstance(summaries, list) and len(summaries) == 6
    rows = [
        json.loads(line)
        for line in (tmp_path / "ordinary-observations.jsonl").read_text().splitlines()
    ]
    assert len(rows) == 12 and provider.budget.calls == 36
    assert {row["arm"]["policy_version"] for row in rows} == {"formation@9", "formation@11"}
    assert not closed
    await provider.close()
    assert closed
