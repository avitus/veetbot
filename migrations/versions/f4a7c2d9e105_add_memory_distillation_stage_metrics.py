"""Add content-free provider stage metrics to memory consolidation audits.

Revision ID: f4a7c2d9e105
Revises: e3a1c5d7f9b2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f4a7c2d9e105"
down_revision: str | Sequence[str] | None = "e3a1c5d7f9b2"
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
