"""Add durable capability scenario and criterion results.

Revision ID: e1f4a8c9b2d3
Revises: e1a4b7c9d205
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f4a8c9b2d3"
down_revision: str | Sequence[str] | None = "e1a4b7c9d205"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "eval_scenario_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scenario_id", sa.Text(), nullable=False),
        sa.Column("suite", sa.Text(), nullable=False),
        sa.Column("repeat_index", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("judge_version", sa.Text(), nullable=False),
        sa.Column("build_ref", sa.Text(), nullable=False),
        sa.Column("score", sa.Numeric(), nullable=True),
        sa.Column("ceiling_hit", sa.Text(), nullable=True),
        sa.Column("policy_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_scenario_runs")),
        sa.UniqueConstraint(
            "scenario_id",
            "build_ref",
            "judge_version",
            "repeat_index",
            name="uq_eval_scenario_run_build_repeat",
        ),
    )
    op.create_index(
        "ix_eval_scenario_runs_suite_build",
        "eval_scenario_runs",
        ["suite", "build_ref", "judge_version"],
    )
    op.create_index(
        "ix_eval_scenario_runs_started",
        "eval_scenario_runs",
        ["started_at"],
    )
    op.create_table(
        "eval_criterion_scores",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("scenario_run_id", sa.Uuid(), nullable=False),
        sa.Column("criterion", sa.Text(), nullable=False),
        sa.Column("observation", sa.Text(), nullable=False),
        sa.Column("value", sa.Numeric(), nullable=False),
        sa.ForeignKeyConstraint(
            ["scenario_run_id"],
            ["eval_scenario_runs.id"],
            name=op.f("fk_eval_criterion_scores_scenario_run_id_eval_scenario_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_eval_criterion_scores")),
        sa.UniqueConstraint(
            "scenario_run_id",
            "criterion",
            name="uq_eval_criterion_scenario_run",
        ),
    )


def downgrade() -> None:
    op.drop_table("eval_criterion_scores")
    op.drop_index("ix_eval_scenario_runs_started", table_name="eval_scenario_runs")
    op.drop_index("ix_eval_scenario_runs_suite_build", table_name="eval_scenario_runs")
    op.drop_table("eval_scenario_runs")
