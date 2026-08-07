"""Index the general-artifact expiry sweep.

Revision ID: f2a6d74b9c10
Revises: b8d7f46291ac
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a6d74b9c10"
down_revision: str | Sequence[str] | None = "b8d7f46291ac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_GENERAL_ARTIFACT_PREDICATE = "origin <> 'trajectory_export' AND expires_at IS NOT NULL"


def upgrade() -> None:
    op.create_index(
        "ix_artifacts_general_expires_at",
        "artifacts",
        ["expires_at"],
        postgresql_where=sa.text(_GENERAL_ARTIFACT_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index("ix_artifacts_general_expires_at", table_name="artifacts")
