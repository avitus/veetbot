from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from agent_core.bootstrap import build
from agent_core.domain.evaluations import EvalCriterionScore, EvalScenarioRun
from tests.integration.m2_support import database_settings

NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _run(row_id: int, score: str) -> EvalScenarioRun:
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
        cost_usd=Decimal("0.05"),
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=2),
    )


async def test_postgres_capability_repeat_replaces_scores_by_build_key() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        initial = _run(1, "0.5")
        async with composition.uow_factory() as uow:
            saved = await uow.evaluations.replace(
                initial,
                [
                    EvalCriterionScore(
                        id=UUID(int=200),
                        scenario_run_id=initial.id,
                        criterion="correctness",
                        observation="Partly correct.",
                        value=Decimal("2"),
                    )
                ],
            )
        assert not saved.replaced

        rerun = _run(2, "1")
        async with composition.uow_factory() as uow:
            replaced = await uow.evaluations.replace(
                rerun,
                [
                    EvalCriterionScore(
                        id=UUID(int=201),
                        scenario_run_id=rerun.id,
                        criterion="correctness",
                        observation="Correct.",
                        value=Decimal("4"),
                    )
                ],
            )
        assert replaced.replaced
        assert replaced.run.id == initial.id

        async with composition.uow_factory() as uow:
            distribution = await uow.evaluations.list_for_build("research", "abc123", "judge.v1")
            spend = await uow.evaluations.cost_since(NOW - timedelta(days=1))
        assert len(distribution) == 1
        assert distribution[0].run.score == Decimal("1")
        assert distribution[0].criteria[0].observation == "Correct."
        assert spend == Decimal("0.05")
