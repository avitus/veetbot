"""Remember how a website login's latest sign-in began (ADR-0297).

The app reopens that way, because a site such as Chase refuses the device window
while a login saved through the remote browser still needs signing in again. A
login that existed before this keeps NULL, and the app opens the device window
as it did.

Revision ID: e6b2d9f4a1c7
Revises: c3d8f1a7b2e4
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6b2d9f4a1c7"
down_revision: str | Sequence[str] | None = "c3d8f1a7b2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("browser_profiles", sa.Column("last_sign_in_mode", sa.Text(), nullable=True))
    op.create_check_constraint(
        "last_sign_in_mode_closed",
        "browser_profiles",
        "last_sign_in_mode IS NULL OR last_sign_in_mode IN ('remote','device')",
    )


def downgrade() -> None:
    op.drop_constraint("last_sign_in_mode_closed", "browser_profiles", type_="check")
    op.drop_column("browser_profiles", "last_sign_in_mode")
