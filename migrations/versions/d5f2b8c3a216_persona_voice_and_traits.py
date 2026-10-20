"""Add the persona voice and lean-towards traits (ADR-0216 part A).

Both belong to a document version. Versions written before them load with no
voice and no traits, which renders exactly as before, so nothing is backfilled.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d5f2b8c3a216"
down_revision: str | Sequence[str] | None = "c4e7a1d9b215"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("persona_documents", sa.Column("voice", sa.Text(), nullable=True))
    op.add_column(
        "persona_documents",
        sa.Column(
            "traits",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("persona_documents", "traits")
    op.drop_column("persona_documents", "voice")
