"""Static checks for query-critical persistence indexes."""

from typing import cast

from sqlalchemy import Table

from agent_core.adapters.persistence.sqlalchemy_models import EvalScenarioAttemptCostRow


def test_capability_attempt_costs_index_the_scenario_run_foreign_key() -> None:
    table = cast(Table, EvalScenarioAttemptCostRow.__table__)
    indexed_columns = {tuple(column.name for column in index.columns) for index in table.indexes}

    assert ("scenario_run_id",) in indexed_columns
