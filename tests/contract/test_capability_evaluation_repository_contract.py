from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from agent_core.adapters.persistence.memory import InMemoryCapabilityEvaluationRepository
from agent_core.domain.evaluations import EvalCriterionScore, EvalScenarioRun
from agent_core.ports.repositories import CapabilityEvaluationRepository

NOW = datetime(2026, 8, 17, tzinfo=UTC)


def scenario_run(
    *,
    row_id: int = 1,
    score: str | None = "0.75",
    scenario_id: str = "cap-research-0001",
    repeat_index: int = 0,
    ceiling_hit: str | None = None,
) -> EvalScenarioRun:
    return EvalScenarioRun(
        id=UUID(int=row_id),
        scenario_id=scenario_id,
        suite="research",
        repeat_index=repeat_index,
        run_id=UUID(int=100 + row_id),
        judge_version="judge.v1",
        build_ref="abc123",
        score=None if score is None else Decimal(score),
        ceiling_hit=ceiling_hit,
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


async def assert_capability_result_replacement(
    repository: CapabilityEvaluationRepository,
) -> None:
    initial = scenario_run()
    saved = await repository.replace(initial, [criterion(initial)])
    assert not saved.replaced
    loaded = await repository.get_by_key(
        initial.scenario_id,
        initial.build_ref,
        initial.judge_version,
        initial.repeat_index,
    )
    assert loaded is not None
    assert loaded.run.score == Decimal("0.75")

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
    assert await repository.cost_since(NOW - timedelta(seconds=1)) == Decimal("0.24")

    await repository.replace(rerun, [criterion(rerun, value="4")])
    assert await repository.cost_since(NOW - timedelta(seconds=1)) == Decimal("0.24")


async def assert_capability_ceiling_hit(repository: CapabilityEvaluationRepository) -> None:
    run = scenario_run(
        row_id=3,
        score=None,
        scenario_id="cap-research-ceiling-0001",
        ceiling_hit="model_calls",
    )
    await repository.replace(run, [])
    loaded = await repository.get_by_key(
        run.scenario_id, run.build_ref, run.judge_version, run.repeat_index
    )
    assert loaded is not None
    assert loaded.run.score is None
    assert loaded.run.ceiling_hit == "model_calls"
    assert loaded.criteria == []


async def test_capability_result_replaces_one_build_repeat_without_inflation() -> None:
    await assert_capability_result_replacement(InMemoryCapabilityEvaluationRepository())


async def test_ceiling_hit_is_unscored_and_queryable() -> None:
    await assert_capability_ceiling_hit(InMemoryCapabilityEvaluationRepository())
