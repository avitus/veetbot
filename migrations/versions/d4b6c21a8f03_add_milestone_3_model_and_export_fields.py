"""Add Milestone 3 model metadata, run pins, and export retention.

Revision ID: d4b6c21a8f03
Revises: 50f3618a97d0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d4b6c21a8f03"
down_revision: str | Sequence[str] | None = "50f3618a97d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the scalar fields required by Milestone 3 hard gates."""

    op.add_column(
        "runs",
        sa.Column("provider_pin", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "model_calls",
        sa.Column(
            "provider_api",
            sa.Text(),
            server_default=sa.text("'chat_completions'"),
            nullable=False,
        ),
    )
    op.add_column("model_calls", sa.Column("response_id", sa.Text(), nullable=True))
    op.add_column("model_calls", sa.Column("request_id", sa.Text(), nullable=True))
    op.add_column("model_calls", sa.Column("resolved_model", sa.Text(), nullable=True))
    op.add_column(
        "model_calls",
        sa.Column(
            "cache_breakpoints_sent",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "model_calls",
        sa.Column(
            "cache_breakpoints_dropped",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_model_calls_tenant_response",
        "model_calls",
        ["tenant_id", "response_id"],
        unique=False,
    )
    op.add_column(
        "artifacts",
        sa.Column(
            "origin",
            sa.Text(),
            server_default=sa.text("'trajectory_export'"),
            nullable=False,
        ),
    )
    op.add_column(
        "artifacts",
        sa.Column(
            "trust",
            sa.Text(),
            server_default=sa.text("'external_untrusted'"),
            nullable=False,
        ),
    )
    op.add_column("artifacts", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_artifacts_expires_at", "artifacts", ["expires_at"], unique=False)


def downgrade() -> None:
    """Remove the Milestone 3 additive fields."""

    op.drop_index("ix_artifacts_expires_at", table_name="artifacts")
    op.drop_column("artifacts", "expires_at")
    op.drop_column("artifacts", "trust")
    op.drop_column("artifacts", "origin")
    op.drop_index("ix_model_calls_tenant_response", table_name="model_calls")
    op.drop_column("model_calls", "cache_breakpoints_dropped")
    op.drop_column("model_calls", "cache_breakpoints_sent")
    op.drop_column("model_calls", "resolved_model")
    op.drop_column("model_calls", "request_id")
    op.drop_column("model_calls", "response_id")
    op.drop_column("model_calls", "provider_api")
    op.drop_column("runs", "provider_pin")
