"""Establish the linear migration graph.

Revision ID: a3f19c2b7d04
Revises: None
"""

from collections.abc import Sequence

revision: str = "a3f19c2b7d04"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create no application tables during the repository milestone."""


def downgrade() -> None:
    """Remove no application tables during the repository milestone."""
