"""Admit one scoped folder grant on a registered Mac."""

from alembic import op

revision = "b4e8c1a7d924"
down_revision = "b4e8c1a7d923"
branch_labels = None
depends_on = None

_NAME = "ck_devices_device_capabilities_closed"


def upgrade() -> None:
    op.drop_constraint(op.f(_NAME), "devices", type_="check")
    op.create_check_constraint(
        op.f(_NAME),
        "devices",
        "jsonb_typeof(capabilities) = 'array' AND ("
        "capabilities <@ '[\"device.sms.send\"]'::jsonb OR ("
        "kind IN ('desktop','laptop') AND platform = 'macos' "
        "AND jsonb_array_length(capabilities) = 1 "
        "AND jsonb_typeof(capabilities->0) = 'string' "
        "AND (capabilities->>0) ~ '^device[.]folder[.]write_[a-f0-9]{32}$'))",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE devices SET capabilities = '[]'::jsonb "
        "WHERE capabilities->>0 LIKE 'device.folder.%'"
    )
    op.drop_constraint(op.f(_NAME), "devices", type_="check")
    op.create_check_constraint(
        op.f(_NAME),
        "devices",
        """jsonb_typeof(capabilities) = 'array' AND capabilities <@ '["device.sms.send"]'::jsonb""",
    )
