"""Index the recall-trace operator-field expiry sweep.

Revision ID: a3f5c81b7d24
Revises: c7e9a4f2d105
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a3f5c81b7d24"
down_revision: str | Sequence[str] | None = "c7e9a4f2d105"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_recall_traces_operator_expiry",
        "recall_traces",
        ["operator_fields_expire_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_recall_traces_operator_expiry", table_name="recall_traces")
