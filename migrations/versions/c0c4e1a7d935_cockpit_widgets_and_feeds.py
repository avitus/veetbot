"""Add Cockpit widgets, revisions, settings, layouts, feeds, events and the outbox (Milestone 35).

Revision ID: c0c4e1a7d935
Revises: f4b7d1c8a903
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c0c4e1a7d935"
down_revision: str | Sequence[str] | None = "f4b7d1c8a903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = (
    "cockpit_import_failures",
    "cockpit_commit_outbox",
    "cockpit_events",
    "cockpit_feed_snapshots",
    "cockpit_feeds",
    "cockpit_layouts",
    "cockpit_widget_settings",
    "cockpit_widget_revisions",
    "cockpit_widgets",
)
_OWNER = "tenant_id TEXT NOT NULL, principal_id TEXT NOT NULL"
_WIDGET_FK = (
    "FOREIGN KEY (tenant_id, principal_id, slug) REFERENCES cockpit_widgets "
    "(tenant_id, principal_id, slug) ON DELETE CASCADE"
)


_TENANT = (
    "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
    "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
)


def upgrade() -> None:
    op.execute(
        f"CREATE TABLE cockpit_widgets ({_OWNER}, slug TEXT NOT NULL, state TEXT NOT NULL, "
        "live_revision_id UUID, created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "updated_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "CONSTRAINT pk_cockpit_widgets PRIMARY KEY (tenant_id, principal_id, slug), "
        "CONSTRAINT ck_cockpit_widgets_state CHECK (state IN ('active', 'deleted')))"
    )
    op.execute(
        f"CREATE TABLE cockpit_widget_revisions ({_OWNER}, id UUID NOT NULL, slug TEXT NOT NULL, "
        "source TEXT NOT NULL, state TEXT NOT NULL, files JSONB NOT NULL, "
        "diagnostics JSONB NOT NULL, bundle TEXT, bundle_sha256 TEXT, kit_api INTEGER, "
        "commit_sha TEXT, origin_run_id UUID, created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "updated_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "CONSTRAINT pk_cockpit_widget_revisions PRIMARY KEY (tenant_id, principal_id, id), "
        f"CONSTRAINT fk_cockpit_widget_revisions_tenant_id_cockpit_widgets {_WIDGET_FK}, "
        "CONSTRAINT ck_cockpit_widget_revisions_state CHECK (state IN ('validating', "
        "'rejected', 'pending_commit', 'committed', 'live', 'superseded', 'conflicted')), "
        "CONSTRAINT ck_cockpit_widget_revisions_source CHECK (source IN ('assistant', "
        "'owner_git', 'revert')), "
        "CONSTRAINT ck_cockpit_widget_revisions_files CHECK (jsonb_typeof(files) = 'object'), "
        "CONSTRAINT ck_cockpit_widget_revisions_diagnostics CHECK "
        "(jsonb_typeof(diagnostics) = 'array'))"
    )
    op.execute(
        "CREATE INDEX ix_cockpit_widget_revisions_widget_created ON cockpit_widget_revisions "
        "(tenant_id, principal_id, slug, created_at)"
    )
    op.execute(
        "CREATE INDEX ix_cockpit_widget_revisions_bundle ON cockpit_widget_revisions "
        "(tenant_id, principal_id, bundle_sha256)"
    )
    op.execute(
        f"CREATE TABLE cockpit_widget_settings ({_OWNER}, slug TEXT NOT NULL, "
        "values JSONB NOT NULL, updated_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "CONSTRAINT pk_cockpit_widget_settings PRIMARY KEY (tenant_id, principal_id, slug), "
        f"CONSTRAINT fk_cockpit_widget_settings_tenant_id_cockpit_widgets {_WIDGET_FK}, "
        "CONSTRAINT ck_cockpit_widget_settings_values CHECK (jsonb_typeof(values) = 'object'))"
    )
    op.execute(
        f"CREATE TABLE cockpit_layouts ({_OWNER}, device_class TEXT NOT NULL, "
        "placements JSONB NOT NULL, revision INTEGER NOT NULL, "
        "updated_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "CONSTRAINT pk_cockpit_layouts PRIMARY KEY (tenant_id, principal_id, device_class), "
        "CONSTRAINT ck_cockpit_layouts_device_class CHECK (device_class IN ('compact', "
        "'regular', 'wide')), "
        "CONSTRAINT ck_cockpit_layouts_placements CHECK (jsonb_typeof(placements) = 'array'), "
        "CONSTRAINT ck_cockpit_layouts_revision CHECK (revision >= 1))"
    )
    op.execute(
        f"CREATE TABLE cockpit_feeds ({_OWNER}, id TEXT NOT NULL, schema JSONB NOT NULL, "
        "schema_version INTEGER NOT NULL, trust TEXT NOT NULL, "
        "created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "updated_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "CONSTRAINT pk_cockpit_feeds PRIMARY KEY (tenant_id, principal_id, id), "
        "CONSTRAINT ck_cockpit_feeds_trust CHECK (trust IN ('platform', 'untrusted')), "
        "CONSTRAINT ck_cockpit_feeds_schema_version CHECK (schema_version >= 1))"
    )
    op.execute(
        f"CREATE TABLE cockpit_feed_snapshots ({_OWNER}, feed_id TEXT NOT NULL, "
        "sequence BIGINT NOT NULL, observed_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "expected_interval_seconds INTEGER NOT NULL, error TEXT, data JSONB NOT NULL, "
        "schema_version INTEGER NOT NULL, recorded_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "CONSTRAINT pk_cockpit_feed_snapshots PRIMARY KEY "
        "(tenant_id, principal_id, feed_id, sequence), "
        "CONSTRAINT fk_cockpit_feed_snapshots_tenant_id_cockpit_feeds FOREIGN KEY "
        "(tenant_id, principal_id, feed_id) REFERENCES cockpit_feeds "
        "(tenant_id, principal_id, id) ON DELETE CASCADE, "
        "CONSTRAINT ck_cockpit_feed_snapshots_interval CHECK (expected_interval_seconds >= 1))"
    )
    op.execute(
        "CREATE TABLE cockpit_events (sequence BIGINT GENERATED BY DEFAULT AS IDENTITY NOT NULL, "
        f"{_OWNER}, kind TEXT NOT NULL, subject TEXT NOT NULL, "
        "occurred_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "CONSTRAINT pk_cockpit_events PRIMARY KEY (sequence))"
    )
    op.execute(
        "CREATE INDEX ix_cockpit_events_owner_sequence ON cockpit_events "
        "(tenant_id, principal_id, sequence)"
    )
    op.execute(
        "CREATE INDEX ix_cockpit_events_tenant_occurred ON cockpit_events (tenant_id, occurred_at)"
    )
    op.execute(
        f"CREATE TABLE cockpit_commit_outbox ({_OWNER}, revision_id UUID NOT NULL, "
        "slug TEXT NOT NULL, operation TEXT NOT NULL, attempts INTEGER NOT NULL, "
        "next_attempt_at TIMESTAMP WITH TIME ZONE NOT NULL, last_error TEXT, "
        "expected_parent_sha TEXT, created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "CONSTRAINT pk_cockpit_commit_outbox PRIMARY KEY (tenant_id, principal_id, revision_id), "
        "CONSTRAINT ck_cockpit_commit_outbox_operation CHECK (operation IN ('write', 'remove')), "
        "CONSTRAINT ck_cockpit_commit_outbox_attempts CHECK (attempts >= 0))"
    )
    op.execute(
        "CREATE INDEX ix_cockpit_commit_outbox_due ON cockpit_commit_outbox "
        "(tenant_id, next_attempt_at)"
    )
    op.execute(
        f"CREATE TABLE cockpit_import_failures ({_OWNER}, id UUID NOT NULL, "
        "commit_sha TEXT NOT NULL, slug TEXT, diagnostics JSONB NOT NULL, seen BOOLEAN NOT NULL, "
        "created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "CONSTRAINT pk_cockpit_import_failures PRIMARY KEY (tenant_id, principal_id, id), "
        "CONSTRAINT ck_cockpit_import_failures_diagnostics CHECK "
        "(jsonb_typeof(diagnostics) = 'array'))"
    )
    op.execute(
        "CREATE INDEX ix_cockpit_import_failures_owner_created ON cockpit_import_failures "
        "(tenant_id, principal_id, created_at)"
    )
    op.execute("ALTER TABLE cockpit_widgets ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cockpit_widgets FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY cockpit_widgets_tenant_isolation ON cockpit_widgets {_TENANT}")
    op.execute("ALTER TABLE cockpit_widget_revisions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cockpit_widget_revisions FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY cockpit_widget_revisions_tenant_isolation ON cockpit_widget_revisions "
        f"{_TENANT}"
    )
    op.execute("ALTER TABLE cockpit_widget_settings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cockpit_widget_settings FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY cockpit_widget_settings_tenant_isolation ON cockpit_widget_settings "
        f"{_TENANT}"
    )
    op.execute("ALTER TABLE cockpit_layouts ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cockpit_layouts FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY cockpit_layouts_tenant_isolation ON cockpit_layouts {_TENANT}")
    op.execute("ALTER TABLE cockpit_feeds ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cockpit_feeds FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY cockpit_feeds_tenant_isolation ON cockpit_feeds {_TENANT}")
    op.execute("ALTER TABLE cockpit_feed_snapshots ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cockpit_feed_snapshots FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY cockpit_feed_snapshots_tenant_isolation ON cockpit_feed_snapshots {_TENANT}"
    )
    op.execute("ALTER TABLE cockpit_events ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cockpit_events FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY cockpit_events_tenant_isolation ON cockpit_events {_TENANT}")
    op.execute("ALTER TABLE cockpit_commit_outbox ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cockpit_commit_outbox FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY cockpit_commit_outbox_tenant_isolation ON cockpit_commit_outbox {_TENANT}"
    )
    op.execute("ALTER TABLE cockpit_import_failures ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE cockpit_import_failures FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY cockpit_import_failures_tenant_isolation ON cockpit_import_failures "
        f"{_TENANT}"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    held = " OR ".join(f"EXISTS (SELECT 1 FROM {table})" for table in TABLES)
    op.execute(
        f"DO $$ BEGIN IF {held} THEN "
        "RAISE EXCEPTION 'Cockpit data must be exported and erased before downgrade'; "
        "END IF; END $$"
    )
    for table in TABLES:
        op.drop_table(table)
