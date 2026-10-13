"""Version-bound derived vectors; the original memory remains authoritative."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql

revision: str = "ea3310a1b001"
down_revision: str | Sequence[str] | None = "ea3210a1b00c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "memory_vectors",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("belief_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("space_key", sa.Text(), nullable=False),
        sa.Column("space", postgresql.JSONB(), nullable=False),
        sa.Column("content_revision", sa.BigInteger(), nullable=False),
        sa.Column("creation_sequence", sa.BigInteger(), nullable=False),
        sa.Column("content_digest", sa.Text(), nullable=False),
        sa.Column("embedding", VECTOR(), nullable=False),
        sa.PrimaryKeyConstraint("tenant_id", "principal_id", "belief_id", "space_key"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "principal_id", "belief_id"],
            ["memories.tenant_id", "memories.principal_id", "memories.id"],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("content_revision > 0 AND creation_sequence > 0", name="source_version"),
        sa.CheckConstraint(
            "vector_dims(embedding) = (space->>'dimensions')::int", name="dimensions"
        ),
    )
    op.execute("ALTER TABLE memory_vectors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memory_vectors FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY memory_vectors_tenant_isolation ON memory_vectors "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    # Only derived acceleration is discarded. Never remove the shared extension.
    op.drop_table("memory_vectors")
