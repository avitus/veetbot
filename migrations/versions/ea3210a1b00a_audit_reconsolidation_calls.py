"""Retain closed provider-call audit alongside its existing spend reservation."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "ea3210a1b00a"
down_revision: str | Sequence[str] | None = "ea3210a1b009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "reconsolidation_spend",
        sa.Column("call_audit", postgresql.JSONB(none_as_null=True), nullable=True),
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "LOCK TABLE reconsolidation_owners, reconsolidation_jobs, "
        "reconsolidation_spend IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM reconsolidation_jobs WHERE state = 'running') "
            "OR EXISTS (SELECT 1 FROM reconsolidation_spend WHERE call_audit IS NOT NULL)"
        )
    ):
        raise RuntimeError(
            "Disable reconsolidation, finish leased work and export call audits "
            "before downgrade; provider audit history must not be lost"
        )
    op.drop_column("reconsolidation_spend", "call_audit")
