"""Admit hypothesis/conflict payloads in existing owner-bound operation storage."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ea3210a1b00b"
down_revision: str | Sequence[str] | None = "ea3210a1b00a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Payloads use existing operation, dependency and derived projection tables.
    pass


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "LOCK TABLE reconsolidation_owners, reconsolidation_jobs, "
        "reconsolidation_operations IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM reconsolidation_jobs WHERE state = 'running') "
            "OR EXISTS (SELECT 1 FROM reconsolidation_operations "
            "WHERE payload->>'kind' IN ('hypothesis', 'conflict'))"
        )
    ):
        raise RuntimeError(
            "Disable reconsolidation and preserve hypothesis/conflict history before downgrade; "
            "previous readers cannot decode these operations"
        )
