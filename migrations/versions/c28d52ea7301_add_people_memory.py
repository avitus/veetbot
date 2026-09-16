"""Add source-backed People identities and immutable revisions.

Revision ID: c28d52ea7301
Revises: b27c41d9e602
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c28d52ea7301"
down_revision: str | Sequence[str] | None = "b27c41d9e602"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE people_heads (  tenant_id TEXT NOT NULL,   principal_id TEXT NOT NULL, "
        "id UUID NOT NULL,   kind TEXT NOT NULL,   revision INTEGER NOT NULL,   sensitivity "
        "INTEGER NOT NULL,   erased BOOLEAN NOT NULL,   excluded BOOLEAN NOT NULL, "
        "CONSTRAINT pk_people_heads PRIMARY KEY (tenant_id, principal_id, id),   CONSTRAINT "
        "ck_people_heads_people_head_revision_positive CHECK (revision > 0),   CONSTRAINT "
        "ck_people_heads_people_head_sensitivity CHECK (sensitivity BETWEEN 0 AND 3), "
        "CONSTRAINT ck_people_heads_people_head_kind CHECK (kind IN "
        "('person','identifier','source','mention','memory_link','organization',"
        "'relationship','interaction','commitment','operation','erasure','import_job')) "
        ")"
    )
    op.execute("ALTER TABLE people_heads ENABLE ROW LEVEL SECURITY")
    op.execute(
        "CREATE INDEX ix_people_head_sensitivity ON people_heads "
        "(tenant_id, principal_id, sensitivity)"
    )
    op.execute(
        "CREATE INDEX ix_people_head_hidden ON people_heads "
        "(tenant_id, principal_id, id) WHERE erased OR excluded"
    )
    op.execute("ALTER TABLE people_heads FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY people_heads_tenant_isolation ON people_heads USING (tenant_id = "
        "current_setting('agent_core.tenant_id', true)) WITH CHECK (tenant_id = "
        "current_setting('agent_core.tenant_id', true))"
    )
    op.execute(
        "CREATE TABLE people_revisions (  tenant_id TEXT NOT NULL,   principal_id TEXT NOT "
        "NULL,   entity_id UUID NOT NULL,   revision INTEGER NOT NULL,   kind TEXT NOT NULL, "
        "recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,   event_at TIMESTAMP WITH TIME ZONE "
        "NOT NULL,   sensitivity INTEGER NOT NULL,   search_text TEXT NOT NULL, "
        "source_session_id UUID,   payload JSONB NOT NULL,   CONSTRAINT pk_people_revisions "
        "PRIMARY KEY (tenant_id, principal_id, entity_id, revision),   CONSTRAINT "
        "fk_people_revisions_tenant_id_people_heads FOREIGN KEY(tenant_id, principal_id, "
        "entity_id) REFERENCES people_heads (tenant_id, principal_id, id) ON DELETE CASCADE, "
        "CONSTRAINT ck_people_revisions_people_revision_positive CHECK (revision > 0), "
        "CONSTRAINT ck_people_revisions_people_revision_payload CHECK (jsonb_typeof(payload) "
        "= 'object' AND payload->>'kind' = kind),   CONSTRAINT "
        "ck_people_revisions_people_revision_sensitivity CHECK (sensitivity BETWEEN 0 AND 3) "
        ")"
    )
    op.execute(
        "CREATE INDEX ix_people_revision_history ON people_revisions (tenant_id, "
        "principal_id, kind, event_at, entity_id)"
    )
    op.execute(
        "CREATE INDEX ix_people_revision_kind_name ON people_revisions (tenant_id, "
        "principal_id, kind, search_text)"
    )
    op.execute(
        "CREATE INDEX ix_people_revision_source_session ON people_revisions (tenant_id, "
        "principal_id, source_session_id)"
    )
    op.execute(
        "CREATE INDEX ix_people_revision_belief ON people_revisions "
        "(tenant_id, principal_id, (payload->>'belief_id'))"
    )
    op.execute("CREATE INDEX ix_events_session_created_id ON events (session_id, created_at, id)")
    op.execute("ALTER TABLE people_revisions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE people_revisions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY people_revisions_tenant_isolation ON people_revisions USING (tenant_id "
        "= current_setting('agent_core.tenant_id', true)) WITH CHECK (tenant_id = "
        "current_setting('agent_core.tenant_id', true))"
    )
    op.execute(
        "CREATE TABLE people_links (  tenant_id TEXT NOT NULL,   principal_id TEXT NOT NULL, "
        "entity_id UUID NOT NULL,   revision INTEGER NOT NULL,   target_id UUID NOT NULL, "
        "role TEXT NOT NULL,   CONSTRAINT pk_people_links PRIMARY KEY (tenant_id, "
        "principal_id, entity_id, revision, target_id, role),   CONSTRAINT "
        "fk_people_links_tenant_id_people_revisions FOREIGN KEY(tenant_id, principal_id, "
        "entity_id, revision) REFERENCES people_revisions (tenant_id, principal_id, "
        "entity_id, revision) ON DELETE CASCADE,   CONSTRAINT "
        "fk_people_links_tenant_id_people_heads FOREIGN KEY(tenant_id, principal_id, "
        "target_id) REFERENCES people_heads (tenant_id, principal_id, id) ON DELETE CASCADE, "
        "CONSTRAINT ck_people_links_people_link_role "
        "CHECK (role IN ('person','source','organization','assignment')) )"
    )
    op.execute(
        "CREATE INDEX ix_people_link_target ON people_links (tenant_id, principal_id, "
        "target_id, role, entity_id)"
    )
    op.execute("ALTER TABLE people_links ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE people_links FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY people_links_tenant_isolation ON people_links USING (tenant_id = "
        "current_setting('agent_core.tenant_id', true)) WITH CHECK (tenant_id = "
        "current_setting('agent_core.tenant_id', true))"
    )

    op.create_unique_constraint(
        "uq_memories_tenant_principal_id", "memories", ["tenant_id", "principal_id", "id"]
    )
    op.execute(
        "CREATE TABLE memory_revisions (id BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
        "tenant_id TEXT NOT NULL, principal_id TEXT NOT NULL, belief_id UUID NOT NULL "
        ", recorded_at TIMESTAMPTZ NOT NULL, payload JSONB NOT NULL, "
        "CONSTRAINT fk_memory_revisions_tenant_id_memories "
        "FOREIGN KEY (tenant_id, principal_id, belief_id) "
        "REFERENCES memories(tenant_id, principal_id, id) ON DELETE CASCADE)"
    )
    op.execute(
        "CREATE INDEX ix_memory_revision_recorded ON memory_revisions "
        "(tenant_id, principal_id, belief_id, recorded_at, id)"
    )
    op.execute("ALTER TABLE memory_revisions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE memory_revisions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY memory_revisions_tenant_isolation ON memory_revisions "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )
    # A constrained migration connection must fail rather than silently skip
    # another tenant's rows during backfill.
    op.execute("SET LOCAL row_security = off")
    # Existing contents establish what is known at migration, not invented earlier revisions.
    op.execute(
        "INSERT INTO memory_revisions (tenant_id, principal_id, belief_id, recorded_at, payload) "
        "SELECT tenant_id, principal_id, id, CURRENT_TIMESTAMP, to_jsonb(memories) FROM memories"
    )

    op.execute("ALTER TABLE memories ADD COLUMN erasure_pending BOOLEAN NOT NULL DEFAULT false")
    op.execute("ALTER TABLE runs ADD COLUMN people_erased_at TIMESTAMPTZ")
    op.execute(
        "ALTER TABLE integrated_episodes ADD COLUMN erasure_pending BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "CREATE INDEX ix_episodes_erasure_pending ON integrated_episodes "
        "(tenant_id, principal_id, id) WHERE erasure_pending"
    )
    op.execute(
        "ALTER TABLE email_records ADD COLUMN erasure_pending BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "CREATE INDEX ix_email_erasure_pending ON email_records "
        "(tenant_id, principal_id, kind, key) WHERE erasure_pending"
    )
    op.execute("ALTER TABLE runs ADD COLUMN erasure_pending BOOLEAN NOT NULL DEFAULT false")
    op.execute(
        "CREATE INDEX ix_runs_erasure_pending ON runs (session_id, id) WHERE erasure_pending"
    )
    op.execute(
        "ALTER TABLE recall_traces ADD COLUMN erasure_pending BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "CREATE INDEX ix_traces_erasure_pending ON recall_traces (tenant_id, principal_id, id) "
        "WHERE erasure_pending"
    )
    for table in ("events", "tool_invocations"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN people_erased BOOLEAN NOT NULL DEFAULT false")
        op.execute(f"ALTER TABLE {table} ADD COLUMN erasure_pending BOOLEAN NOT NULL DEFAULT false")
    op.execute(
        "ALTER TABLE session_history_items ADD COLUMN erasure_cleaned "
        "BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute(
        "CREATE INDEX ix_events_people_erased ON events (session_id, sequence) WHERE people_erased"
    )
    op.execute(
        "CREATE INDEX ix_events_erasure_pending ON events (session_id, id) WHERE erasure_pending"
    )
    op.execute(
        "CREATE INDEX ix_invocations_erasure_pending ON tool_invocations (session_id, id) "
        "WHERE erasure_pending"
    )
    # UUIDs alone form an inverted reference index; names and message text are
    # never copied into a second searchable People representation.
    op.execute(
        "CREATE FUNCTION people_reference_ids(value TEXT) RETURNS UUID[] "
        "LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$ "
        "SELECT coalesce(array_agg(DISTINCT found[1]::uuid ORDER BY found[1]::uuid), "
        "ARRAY[]::uuid[]) FROM regexp_matches(value, "
        "'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}', "
        "'g') AS found $$"
    )
    op.execute(
        "CREATE INDEX ix_events_people_references ON events "
        "USING gin (people_reference_ids(payload::text))"
    )
    op.execute(
        "CREATE INDEX ix_invocations_people_references ON tool_invocations "
        "USING gin (people_reference_ids(coalesce(raw_arguments, '') || "
        "coalesce(arguments::text, '') || coalesce(result_item::text, '') || "
        "coalesce(structured_result::text, '') || coalesce(outcome::text, '') || "
        "coalesce(policy_decision::text, '')))"
    )
    op.execute(
        "CREATE INDEX ix_traces_people_references ON recall_traces "
        "USING gin (people_reference_ids(trace::text))"
    )
    op.execute(
        "CREATE INDEX ix_email_people_beliefs ON email_records "
        "USING gin (people_reference_ids((payload->'memory_ids')::text))"
    )


def downgrade() -> None:
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM people_heads) "
        "OR EXISTS (SELECT 1 FROM memory_revisions) "
        "OR EXISTS (SELECT 1 FROM runs WHERE people_erased_at IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM events WHERE people_erased) "
        "OR EXISTS (SELECT 1 FROM tool_invocations WHERE people_erased) "
        "OR EXISTS (SELECT 1 FROM recall_traces WHERE erasure_pending) "
        "OR EXISTS (SELECT 1 FROM email_records WHERE erasure_pending) "
        "OR EXISTS (SELECT 1 FROM integrated_episodes WHERE erasure_pending) THEN "
        "RAISE EXCEPTION 'People and memory revision data must be "
        "exported and erased before downgrade'; "
        "END IF; END $$"
    )
    op.drop_index("ix_events_session_created_id", table_name="events")
    op.drop_index("ix_events_people_references", table_name="events")
    op.drop_index("ix_invocations_people_references", table_name="tool_invocations")
    op.drop_index("ix_traces_people_references", table_name="recall_traces")
    op.drop_index("ix_email_people_beliefs", table_name="email_records")
    op.execute("DROP FUNCTION people_reference_ids(TEXT)")
    op.drop_table("memory_revisions")
    op.drop_column("memories", "erasure_pending")
    op.drop_column("runs", "people_erased_at")
    op.drop_index("ix_episodes_erasure_pending", table_name="integrated_episodes")
    op.drop_column("integrated_episodes", "erasure_pending")
    op.drop_index("ix_email_erasure_pending", table_name="email_records")
    op.drop_column("email_records", "erasure_pending")
    op.drop_index("ix_runs_erasure_pending", table_name="runs")
    op.drop_column("runs", "erasure_pending")
    op.drop_index("ix_traces_erasure_pending", table_name="recall_traces")
    op.drop_column("recall_traces", "erasure_pending")
    op.drop_index("ix_events_people_erased", table_name="events")
    op.drop_index("ix_events_erasure_pending", table_name="events")
    op.drop_index("ix_invocations_erasure_pending", table_name="tool_invocations")
    for table in ("events", "tool_invocations"):
        op.drop_column(table, "people_erased")
        op.drop_column(table, "erasure_pending")
    op.drop_column("session_history_items", "erasure_cleaned")
    op.drop_constraint("uq_memories_tenant_principal_id", "memories", type_="unique")
    op.drop_table("people_links")
    op.drop_table("people_revisions")
    op.drop_table("people_heads")
