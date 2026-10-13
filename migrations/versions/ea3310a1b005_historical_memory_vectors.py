"""Separate historical vectors, retaining each current-original invalidation fence."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ea3310a1b005"
down_revision: str | Sequence[str] | None = "ea3310a1b004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "memory_vectors",
        sa.Column("historical_revision", sa.BigInteger(), nullable=False, server_default="0"),
    )
    op.drop_constraint("pk_memory_vectors", "memory_vectors", type_="primary")
    op.create_primary_key(
        "pk_memory_vectors",
        "memory_vectors",
        ["tenant_id", "principal_id", "belief_id", "space_key", "historical_revision"],
    )
    op.create_check_constraint("historical_revision", "memory_vectors", "historical_revision >= 0")


def downgrade() -> None:
    # Discard only derived historical acceleration; original revisions remain.
    op.execute("SET LOCAL row_security = off")
    op.execute("DELETE FROM memory_vectors WHERE historical_revision <> 0")
    op.drop_constraint(
        op.f("ck_memory_vectors_historical_revision"), "memory_vectors", type_="check"
    )
    op.drop_constraint("pk_memory_vectors", "memory_vectors", type_="primary")
    op.drop_column("memory_vectors", "historical_revision")
    op.create_primary_key(
        "pk_memory_vectors",
        "memory_vectors",
        ["tenant_id", "principal_id", "belief_id", "space_key"],
    )
