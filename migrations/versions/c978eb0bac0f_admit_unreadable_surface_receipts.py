"""Admit a receipt for an unreadable surface update (ADR-0064 amendment).

Revision ID: c978eb0bac0f
Revises: a13c9e724610
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c978eb0bac0f"
down_revision: str | Sequence[str] | None = "a13c9e724610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_surface_inbound_receipts_surface_receipt_disposition_closed"
_DISPOSITIONS = (
    "'submitted','input_delivered','command_handled',"
    "'rejected_unpaired','rejected_locked','rejected_rate',"
    "'rejected_active_run','rejected_admission','ignored_media',"
    "'ignored_chat_kind'"
)


def upgrade() -> None:
    op.drop_constraint(op.f(_CONSTRAINT), "surface_inbound_receipts", type_="check")
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        "surface_inbound_receipts",
        f"disposition IN ({_DISPOSITIONS},'ignored_unreadable')",
    )


def downgrade() -> None:
    # An unreadable update's receipt keeps its row, because inbound idempotency
    # and the Telegram poll offset depend on it; it becomes an ignored_media
    # receipt, still content-free. The table owner needs RLS lifted to see rows
    # of every tenant, for this one statement.
    op.execute("ALTER TABLE surface_inbound_receipts NO FORCE ROW LEVEL SECURITY")
    op.execute(
        "UPDATE surface_inbound_receipts SET disposition = 'ignored_media' "
        "WHERE disposition = 'ignored_unreadable'"
    )
    op.execute("ALTER TABLE surface_inbound_receipts FORCE ROW LEVEL SECURITY")
    op.drop_constraint(op.f(_CONSTRAINT), "surface_inbound_receipts", type_="check")
    op.create_check_constraint(
        op.f(_CONSTRAINT), "surface_inbound_receipts", f"disposition IN ({_DISPOSITIONS})"
    )
