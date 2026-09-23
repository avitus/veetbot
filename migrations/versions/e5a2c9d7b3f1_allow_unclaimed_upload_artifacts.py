"""Let a chat upload exist before the run of the message that sends it (ADR-0120).

Revision ID: e5a2c9d7b3f1
Revises: b7d2e4f8a613
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a2c9d7b3f1"
down_revision: str | Sequence[str] | None = "b7d2e4f8a613"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_OR_UPLOAD = "run_id IS NOT NULL OR origin IN ('upload', 'knowledge_source')"
_PENDING_INGEST = "(metadata ->> 'auto_ingest') = 'pending'"


def upgrade() -> None:
    op.alter_column("artifacts", "run_id", existing_type=sa.UUID(), nullable=True)
    op.create_check_constraint(
        op.f("ck_artifacts_run_or_upload"),
        "artifacts",
        _RUN_OR_UPLOAD,
    )
    op.create_index(
        "ix_artifacts_upload_ingest_pending",
        "artifacts",
        ["created_at"],
        postgresql_where=sa.text(_PENDING_INGEST),
    )


def downgrade() -> None:
    op.drop_index("ix_artifacts_upload_ingest_pending", table_name="artifacts")
    op.drop_constraint(op.f("ck_artifacts_run_or_upload"), "artifacts", type_="check")
    # Unclaimed uploads have no run to belong to in the older schema.
    op.execute("DELETE FROM artifacts WHERE run_id IS NULL")
    op.alter_column("artifacts", "run_id", existing_type=sa.UUID(), nullable=False)
