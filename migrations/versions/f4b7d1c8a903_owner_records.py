"""Add owner-isolated record series and entries (Milestone 34).

Revision ID: f4b7d1c8a903
Revises: e6a3c9d4b217
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f4b7d1c8a903"
down_revision: str | Sequence[str] | None = "e6a3c9d4b217"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE record_series (  tenant_id TEXT NOT NULL,   principal_id TEXT NOT NULL, "
        "id UUID NOT NULL,   name TEXT NOT NULL,   display_name TEXT NOT NULL, "
        "components JSONB NOT NULL,   sensitivity INTEGER NOT NULL,   cadence TEXT, "
        "revision INTEGER NOT NULL,   created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "updated_at TIMESTAMP WITH TIME ZONE NOT NULL,   CONSTRAINT pk_record_series "
        "PRIMARY KEY (tenant_id, principal_id, id),   CONSTRAINT uq_record_series_name "
        "UNIQUE (tenant_id, principal_id, name),   CONSTRAINT ck_record_series_components "
        "CHECK (jsonb_typeof(components) = 'array' AND jsonb_array_length(components) "
        "BETWEEN 1 AND 4),   CONSTRAINT ck_record_series_sensitivity "
        "CHECK (sensitivity BETWEEN 0 AND 3) )"
    )
    op.execute("ALTER TABLE record_series ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE record_series FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY record_series_tenant_isolation ON record_series USING (tenant_id = "
        "current_setting('agent_core.tenant_id', true)) WITH CHECK (tenant_id = "
        "current_setting('agent_core.tenant_id', true))"
    )
    op.execute(
        "CREATE TABLE record_entries (  tenant_id TEXT NOT NULL,   principal_id TEXT NOT NULL, "
        "id UUID NOT NULL,   series_id UUID NOT NULL,   observed_at TIMESTAMP WITH TIME ZONE "
        "NOT NULL,   observed_precision TEXT NOT NULL,   values JSONB NOT NULL,   note TEXT, "
        "note_withheld BOOLEAN NOT NULL,   idempotency_key TEXT NOT NULL, "
        "source_session_id UUID,   source_event_id UUID,   created_at TIMESTAMP WITH TIME ZONE "
        "NOT NULL,   CONSTRAINT pk_record_entries PRIMARY KEY (tenant_id, principal_id, id), "
        "CONSTRAINT fk_record_entries_tenant_id_record_series FOREIGN KEY(tenant_id, "
        "principal_id, series_id) REFERENCES record_series (tenant_id, principal_id, id) "
        "ON DELETE CASCADE,   CONSTRAINT uq_record_entries_idempotency UNIQUE (tenant_id, "
        "principal_id, series_id, idempotency_key),   CONSTRAINT ck_record_entries_values "
        "CHECK (jsonb_typeof(values) = 'object') )"
    )
    op.execute(
        "CREATE INDEX ix_record_entries_series_observed ON record_entries "
        "(tenant_id, principal_id, series_id, observed_at)"
    )
    op.execute(
        "CREATE INDEX ix_record_entries_source_session ON record_entries (source_session_id)"
    )
    op.execute("ALTER TABLE record_entries ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE record_entries FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY record_entries_tenant_isolation ON record_entries USING (tenant_id = "
        "current_setting('agent_core.tenant_id', true)) WITH CHECK (tenant_id = "
        "current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM record_series) "
        "OR EXISTS (SELECT 1 FROM record_entries) THEN "
        "RAISE EXCEPTION 'Owner record data must be exported and erased before downgrade'; "
        "END IF; END $$"
    )
    op.drop_table("record_entries")
    op.drop_table("record_series")
