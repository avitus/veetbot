"""Allow the bounded candidate batch to commit multiple operations per group."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ea3210a1b009"
down_revision: str | Sequence[str] | None = "ea3210a1b008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_recon_operation_group", "reconsolidation_operations", type_="unique")
    op.create_index(
        "ix_recon_operation_group",
        "reconsolidation_operations",
        ["tenant_id", "principal_id", "group_id"],
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "LOCK TABLE reconsolidation_owners, reconsolidation_jobs, "
        "reconsolidation_operations IN ACCESS EXCLUSIVE MODE"
    )
    if op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM reconsolidation_jobs WHERE state = 'running') "
            "OR EXISTS (SELECT 1 FROM reconsolidation_operations "
            "GROUP BY tenant_id, principal_id, group_id HAVING count(*) > 1)"
        )
    ):
        raise RuntimeError(
            "Disable reconsolidation, finish leased work and export batched operations "
            "before downgrade; group operation history must not be lost"
        )
    op.create_unique_constraint(
        "uq_recon_operation_group",
        "reconsolidation_operations",
        ["tenant_id", "principal_id", "group_id"],
    )
    op.drop_index("ix_recon_operation_group", table_name="reconsolidation_operations")
