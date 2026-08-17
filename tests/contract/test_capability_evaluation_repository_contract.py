from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from agent_core.adapters.persistence.memory import InMemoryCapabilityEvaluationRepository
from agent_core.domain.evaluations import EvalCriterionScore, EvalScenarioRun

NOW = datetime(2026, 8, 17, tzinfo=UTC)


def scenario_run(*, row_id: int = 1, score: str = "0.75") -> EvalScenarioRun:
    return EvalScenarioRun(
        id=UUID(int=row_id),
        scenario_id="cap-research-0001",
        suite="research",
        repeat_index=0,
        run_id=UUID(int=100 + row_id),
        judge_version="judge.v1",
        build_ref="abc123",
        score=Decimal(score),
        policy_failures=0,
        cost_usd=Decimal("0.12"),
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=5),
    )


def criterion(run: EvalScenarioRun, *, value: str = "3") -> EvalCriterionScore:
    return EvalCriterionScore(
        id=UUID(int=200),
        scenario_run_id=run.id,
        criterion="correctness",
        observation="The answer is supported by the supplied evidence.",
        value=Decimal(value),
    )


async def test_capability_result_replaces_one_build_repeat_without_inflation() -> None:
    repository = InMemoryCapabilityEvaluationRepository()
    initial = scenario_run()
    saved = await repository.replace(initial, [criterion(initial)])
    assert not saved.replaced

    rerun = scenario_run(row_id=2, score="0.90")
    replaced = await repository.replace(rerun, [criterion(rerun, value="4")])
    assert replaced.replaced
    assert replaced.run.id == initial.id
    assert replaced.run.run_id == rerun.run_id
    assert replaced.criteria[0].scenario_run_id == initial.id

    distribution = await repository.list_for_build("research", "abc123", "judge.v1")
    assert len(distribution) == 1
    assert distribution[0].run.score == Decimal("0.90")
    assert distribution[0].criteria[0].value == Decimal("4")
    assert await repository.cost_since(NOW - timedelta(seconds=1)) == Decimal("0.12")


async def test_ceiling_hit_is_unscored_and_queryable() -> None:
    repository = InMemoryCapabilityEvaluationRepository()
    run = scenario_run().model_copy(update={"score": None, "ceiling_hit": "model_calls"})
    await repository.replace(run, [])
    loaded = await repository.get_by_key(
        run.scenario_id, run.build_ref, run.judge_version, run.repeat_index
    )
    assert loaded is not None
    assert loaded.run.score is None
    assert loaded.run.ceiling_hit == "model_calls"
    assert loaded.criteria == []
