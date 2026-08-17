"""Preserve cost accounting for replaced capability attempts.

Revision ID: f7a2c9e4d1b6
Revises: e1f4a8c9b2d3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7a2c9e4d1b6"
down_revision: str | Sequence[str] | None = "e1f4a8c9b2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_scenario_attempt_costs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scenario_run_id", sa.Uuid(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["scenario_run_id"],
            ["eval_scenario_runs.id"],
            name=op.f("fk_eval_scenario_attempt_costs_scenario_run_id_eval_scenario_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_scenario_attempt_costs")),
    )
    op.create_index(
        "ix_eval_scenario_attempt_costs_started",
        "eval_scenario_attempt_costs",
        ["started_at"],
    )
    op.execute(
        "INSERT INTO eval_scenario_attempt_costs "
        "(id, scenario_run_id, cost_usd, started_at) "
        "SELECT id, id, cost_usd, started_at FROM eval_scenario_runs"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_eval_scenario_attempt_costs_started",
        table_name="eval_scenario_attempt_costs",
    )
    op.drop_table("eval_scenario_attempt_costs")
