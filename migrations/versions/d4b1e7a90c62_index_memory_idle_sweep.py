"""Index the decay sweep's least-recently-reinforced window.

Revision ID: d4b1e7a90c62
Revises: a3f5c81b7d24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d4b1e7a90c62"
down_revision: str | Sequence[str] | None = "a3f5c81b7d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_memories_principal_idle",
        "memories",
        ["tenant_id", "principal_id", "status", "last_reinforced_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_memories_principal_idle", table_name="memories")
