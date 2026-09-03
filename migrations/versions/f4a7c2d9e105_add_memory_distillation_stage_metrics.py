"""Add content-free provider stage metrics to memory consolidation audits.

Revision ID: f4a7c2d9e105
Revises: f1c7a3e05b28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4a7c2d9e105"
down_revision: str | Sequence[str] | None = "f1c7a3e05b28"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "consolidation_runs",
        sa.Column(
            "provider_stage_metrics",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("consolidation_runs", "provider_stage_metrics")
