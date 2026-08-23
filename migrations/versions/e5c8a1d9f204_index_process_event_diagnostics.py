"""Index principal-scoped process-event diagnostics.

Revision ID: e5c8a1d9f204
Revises: d4b1e7a90c62
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e5c8a1d9f204"
down_revision: str | Sequence[str] | None = "d4b1e7a90c62"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_process_events_diagnostics_scope ON process_events "
        "(event_type, (payload ->> 'tenant_id'), (payload ->> 'principal_id'), "
        "(payload ->> 'session_id'), created_at DESC, id DESC)"
    )


def downgrade() -> None:
    op.drop_index("ix_process_events_diagnostics_scope", table_name="process_events")
