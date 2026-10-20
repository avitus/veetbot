"""Add manual Run-now occurrences beside scheduled ones (ADR-0215 part C)."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e7a1d9b215"
down_revision: str | Sequence[str] | None = "ea3310a1b005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "schedule_occurrences",
        sa.Column("trigger", sa.String(length=16), nullable=False, server_default="SCHEDULED"),
    )
    op.add_column(
        "schedule_occurrences",
        sa.Column("manual_request_key", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_schedule_occurrences_schedule_occurrence_trigger_values"),
        "schedule_occurrences",
        "trigger IN ('SCHEDULED','MANUAL')",
    )
    op.create_check_constraint(
        op.f("ck_schedule_occurrences_schedule_occurrence_manual_key"),
        "schedule_occurrences",
        "(trigger = 'MANUAL') = (manual_request_key IS NOT NULL)",
    )
    # A cadence instant stays unique per schedule; a manual run's nominal
    # instant is its request time and its request key is what is unique.
    op.drop_constraint("uq_schedule_occurrence_nominal", "schedule_occurrences", type_="unique")
    op.create_index(
        "uq_schedule_occurrences_scheduled_nominal",
        "schedule_occurrences",
        ["schedule_id", "nominal_fire_at"],
        unique=True,
        postgresql_where=sa.text("trigger = 'SCHEDULED'"),
    )
    op.create_index(
        "uq_schedule_occurrences_manual_request",
        "schedule_occurrences",
        ["schedule_id", "manual_request_key"],
        unique=True,
        postgresql_where=sa.text("manual_request_key IS NOT NULL"),
    )


def downgrade() -> None:
    # Manual runs keep their sessions and runs; only their ledger rows go.
    op.execute("DELETE FROM schedule_occurrences WHERE trigger = 'MANUAL'")
    op.drop_index("uq_schedule_occurrences_manual_request", table_name="schedule_occurrences")
    op.drop_index("uq_schedule_occurrences_scheduled_nominal", table_name="schedule_occurrences")
    op.create_unique_constraint(
        "uq_schedule_occurrence_nominal",
        "schedule_occurrences",
        ["schedule_id", "nominal_fire_at"],
    )
    op.drop_constraint(
        op.f("ck_schedule_occurrences_schedule_occurrence_manual_key"),
        "schedule_occurrences",
        type_="check",
    )
    op.drop_constraint(
        op.f("ck_schedule_occurrences_schedule_occurrence_trigger_values"),
        "schedule_occurrences",
        type_="check",
    )
    op.drop_column("schedule_occurrences", "manual_request_key")
    op.drop_column("schedule_occurrences", "trigger")
