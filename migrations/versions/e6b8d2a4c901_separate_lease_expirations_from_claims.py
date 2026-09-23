"""Separate expired worker executions from ordinary durable continuations.

Revision ID: e6b8d2a4c901
Revises: a4f7c1e9d2b3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6b8d2a4c901"
down_revision: str | Sequence[str] | None = "a4f7c1e9d2b3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs", sa.Column("lease_expirations", sa.Integer(), server_default="0", nullable=False)
    )
    # Reaper events are atomic with their state changes. Claims and normal
    # suspension/resume events are deliberately not evidence of a crashed worker.
    op.execute(
        """
        UPDATE runs AS r
        SET lease_expirations = history.expirations
        FROM (
            SELECT run_id, count(*) AS expirations
            FROM events
            WHERE actor_type = 'maintenance'
              AND payload ? 'reclaimed_epoch'
              AND (event_type = 'run.requeued'
                   OR (event_type = 'run.failed'
                       AND payload->'failure'->>'reason' = 'max_attempts_exceeded'))
            GROUP BY run_id
        ) AS history
        WHERE r.id = history.run_id
        """
    )
    op.create_check_constraint(
        "run_lease_expirations_nonnegative", "runs", "lease_expirations >= 0"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_runs_run_lease_expirations_nonnegative"), "runs", type_="check")
    op.drop_column("runs", "lease_expirations")
