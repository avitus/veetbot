"""One vector per window of a chunk in the knowledge semantic arm.

The local encoder admits fewer tokens than a chunk holds, so a chunk is
embedded as windows of its text and each window is a row. A chunk the
encoder admits whole keeps one row, window zero, which is what every row
held before.

Revision ID: c3d8f1a7b2e4
Revises: b7e1c4a9d2f3
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d8f1a7b2e4"
down_revision: str | Sequence[str] | None = "b7e1c4a9d2f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "knowledge_chunk_vectors",
        sa.Column("window", sa.Integer(), nullable=False, server_default=sa.text("0")),
    )
    op.drop_constraint("pk_knowledge_chunk_vectors", "knowledge_chunk_vectors", type_="primary")
    op.create_primary_key(
        "pk_knowledge_chunk_vectors", "knowledge_chunk_vectors", ["chunk_id", "space_key", "window"]
    )


def downgrade() -> None:
    # Only derived vectors beyond the first window are discarded; the chunks
    # and their first windows stay.
    op.execute('DELETE FROM knowledge_chunk_vectors WHERE "window" > 0')
    op.drop_constraint("pk_knowledge_chunk_vectors", "knowledge_chunk_vectors", type_="primary")
    op.create_primary_key(
        "pk_knowledge_chunk_vectors", "knowledge_chunk_vectors", ["chunk_id", "space_key"]
    )
    op.drop_column("knowledge_chunk_vectors", "window")
