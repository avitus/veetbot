from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_core.bootstrap import build
from agent_core.domain.evaluations import EvalCriterionScore, EvalScenarioRun
from tests.contract.test_capability_evaluation_repository_contract import (
    assert_capability_ceiling_hit,
    assert_capability_result_replacement,
)
from tests.integration.m2_support import database_settings

NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _run(row_id: int, score: str, *, repeat_index: int = 0) -> EvalScenarioRun:
    return EvalScenarioRun(
        id=UUID(int=row_id),
        scenario_id="cap-research-0001",
        suite="research",
        repeat_index=repeat_index,
        run_id=UUID(int=100 + row_id),
        judge_version="judge.v1",
        build_ref="abc123",
        score=Decimal(score),
        policy_failures=0,
        cost_usd=Decimal("0.05"),
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=2),
    )


async def test_postgres_capability_repository_satisfies_shared_contract() -> None:
    async with (
        build(settings=database_settings(), storage="postgres") as composition,
        composition.uow_factory() as uow,
    ):
        await assert_capability_result_replacement(uow.evaluations)
        await assert_capability_ceiling_hit(uow.evaluations)


async def test_postgres_capability_distribution_batches_criterion_reads() -> None:
    async with build(settings=database_settings(), storage="postgres") as composition:
        async with composition.uow_factory() as uow:
            for row_id, repeat_index in ((10, 0), (11, 1)):
                run = _run(row_id, "0.5", repeat_index=repeat_index)
                await uow.evaluations.replace(
                    run,
                    [
                        EvalCriterionScore(
                            id=UUID(int=200 + row_id),
                            scenario_run_id=run.id,
                            criterion="correctness",
                            observation="Partly correct.",
                            value=Decimal("2"),
                        )
                    ],
                )

        factory = cast(Any, composition.uow_factory)
        engine = cast(AsyncEngine, factory._maker.kw["bind"])
        statements: list[str] = []

        def record_statement(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            statements.append(statement)

        event.listen(engine.sync_engine, "before_cursor_execute", record_statement)
        try:
            async with composition.uow_factory() as uow:
                distribution = await uow.evaluations.list_for_build(
                    "research", "abc123", "judge.v1"
                )
        finally:
            event.remove(engine.sync_engine, "before_cursor_execute", record_statement)

        criterion_selects: Sequence[str] = [
            statement
            for statement in statements
            if statement.lstrip().upper().startswith("SELECT")
            and "eval_criterion_scores" in statement
        ]
        assert len(distribution) == 2
        assert len(criterion_selects) == 1
