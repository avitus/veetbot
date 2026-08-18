"""Index capability attempt costs by their canonical scenario run.

Revision ID: a4c8e2f6b913
Revises: f7a2c9e4d1b6
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a4c8e2f6b913"
down_revision: str | Sequence[str] | None = "f7a2c9e4d1b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_eval_scenario_attempt_costs_scenario_run",
        "eval_scenario_attempt_costs",
        ["scenario_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_eval_scenario_attempt_costs_scenario_run",
        table_name="eval_scenario_attempt_costs",
    )
