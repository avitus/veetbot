"""Index immutable source inventory without blocking existing readers."""

from collections.abc import Sequence

from alembic import op

revision: str = "ea3210a1b004"
down_revision: str | Sequence[str] | None = "ea3210a1b003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_memories_recon_creation",
            "memories",
            ["tenant_id", "principal_id", "creation_sequence", "id"],
            unique=True,
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    # Downgrades can be refused by an earlier data-retention guard. Keep the
    # drop transactional so that refusal also restores this index with the head.
    op.drop_index("ix_memories_recon_creation", table_name="memories", if_exists=True)
