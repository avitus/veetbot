"""Scope HTTP idempotency keys to a tenant principal.

Revision ID: b8d7f46291ac
Revises: e7c4a91f20b6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d7f46291ac"
down_revision: str | Sequence[str] | None = "e7c4a91f20b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("pk_idempotency_keys", "idempotency_keys", type_="primary")
    op.create_primary_key(
        "pk_idempotency_keys",
        "idempotency_keys",
        ["tenant_id", "principal_id", "key"],
    )
    op.create_index(
        "ix_idempotency_keys_expires_at",
        "idempotency_keys",
        ["expires_at"],
    )
    op.drop_index("ix_approvals_tenant_status_created", table_name="approvals")
    op.create_index(
        "ix_approvals_tenant_status_created",
        "approvals",
        ["tenant_id", "status", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_approvals_tenant_status_created", table_name="approvals")
    op.create_index(
        "ix_approvals_tenant_status_created",
        "approvals",
        ["tenant_id", "status", "created_at"],
    )
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_constraint("pk_idempotency_keys", "idempotency_keys", type_="primary")
    # A downgrade cannot represent the scoped-key contract. Retain the newest
    # live representative for each legacy global key before restoring its PK.
    op.execute(
        sa.text(
            "DELETE FROM idempotency_keys AS doomed USING idempotency_keys AS kept "
            "WHERE doomed.key = kept.key AND "
            "(doomed.created_at, doomed.tenant_id, doomed.principal_id) "
            "< (kept.created_at, kept.tenant_id, kept.principal_id)"
        )
    )
    op.create_primary_key("pk_idempotency_keys", "idempotency_keys", ["key"])
