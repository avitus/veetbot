"""Index terminal schedules for bounded retention sweeps.

Revision ID: c9e2a7f4b106
Revises: f4a7c2d9e105
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e2a7f4b106"
down_revision: str | Sequence[str] | None = "f4a7c2d9e105"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_schedules_terminal_retention",
        "schedules",
        ["tenant_id", "updated_at", "id"],
        postgresql_where=sa.text("state IN ('COMPLETED','CANCELLED')"),
    )


def downgrade() -> None:
    op.drop_index("ix_schedules_terminal_retention", table_name="schedules")
