"""Derived chunk vectors for the knowledge semantic arm (ADR-0289).

A row is one chunk's embedding in one space; the chunk row stays the
authority and the vector follows it through the cascade. Row level security
reaches the vector through its chunk, whose own policy reaches the tenant
through the document, so a tenant never ranks against another tenant's vectors.

Revision ID: b7e1c4a9d2f3
Revises: e8a280c10301
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.dialects import postgresql

revision: str = "b7e1c4a9d2f3"
down_revision: str | Sequence[str] | None = "e8a280c10301"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "knowledge_chunk_vectors",
        sa.Column("chunk_id", sa.Text(), nullable=False),
        sa.Column("space_key", sa.Text(), nullable=False),
        sa.Column("space", postgresql.JSONB(), nullable=False),
        sa.Column("text_version", sa.Text(), nullable=False),
        sa.Column("embedding", VECTOR(), nullable=False),
        sa.PrimaryKeyConstraint("chunk_id", "space_key", name=op.f("pk_knowledge_chunk_vectors")),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["knowledge_chunks.chunk_id"],
            name=op.f("fk_knowledge_chunk_vectors_chunk_id_knowledge_chunks"),
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "vector_dims(embedding) = (space->>'dimensions')::int",
            name=op.f("ck_knowledge_chunk_vectors_dimensions"),
        ),
    )
    op.execute("ALTER TABLE knowledge_chunk_vectors ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE knowledge_chunk_vectors FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY knowledge_chunk_vectors_tenant_isolation ON knowledge_chunk_vectors "
        "USING (EXISTS (SELECT 1 FROM knowledge_chunks c "
        "WHERE c.chunk_id = knowledge_chunk_vectors.chunk_id)) "
        "WITH CHECK (EXISTS (SELECT 1 FROM knowledge_chunks c "
        "WHERE c.chunk_id = knowledge_chunk_vectors.chunk_id))"
    )


def downgrade() -> None:
    # Only derived acceleration is discarded. Never remove the shared extension.
    op.drop_table("knowledge_chunk_vectors")
