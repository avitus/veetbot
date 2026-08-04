"""Add Milestone 9 memory, recall trace, and knowledge persistence.

Revision ID: c6d8e9f0a1b2
Revises: 9a71c4e8d2f0
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c6d8e9f0a1b2"
down_revision: str | Sequence[str] | None = "9a71c4e8d2f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _tenant_policy(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {table}_tenant_isolation ON {table} "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) "
        "WITH CHECK (tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def upgrade() -> None:
    op.execute("CREATE SEQUENCE memory_store_position_seq")
    op.create_table(
        "memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("source_session_id", sa.Uuid(), nullable=False),
        sa.Column("source_event_ids", postgresql.JSONB(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sensitivity", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("belief_type", sa.Text(), nullable=False),
        sa.Column("polarity", sa.Text(), nullable=False),
        sa.Column("portability", sa.Text(), nullable=False),
        sa.Column("origin_scopes", postgresql.JSONB(), nullable=False),
        sa.Column("corroboration_count", sa.Integer(), nullable=False),
        sa.Column("last_reinforced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.Column(
            "conflicts_with",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "flagged_for_review", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("formation_run_id", sa.Uuid(), nullable=False),
        sa.Column("consolidation_policy_version", sa.Text(), nullable=False),
        sa.Column("authority", sa.Text(), nullable=False),
        sa.Column("utility", sa.Float(), server_default=sa.text("0"), nullable=False),
        sa.Column("store_position", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["source_session_id"], ["sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["superseded_by"], ["memories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memories")),
        sa.UniqueConstraint("store_position", name=op.f("uq_memories_store_position")),
    )
    op.create_index(
        "ix_memories_principal_live_position",
        "memories",
        ["tenant_id", "principal_id", "status", sa.text("store_position DESC")],
    )
    op.create_index(
        "ix_memories_fts",
        "memories",
        [sa.text("to_tsvector('simple', subject || ' ' || statement)")],
        postgresql_using="gin",
    )
    op.create_table(
        "memory_rejections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("belief_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=True),
        sa.Column("statement_sha256", sa.String(length=64), nullable=False),
        sa.Column("belief_type", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("replacement_id", sa.Uuid(), nullable=True),
        sa.Column("trace_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memory_rejections")),
    )
    op.create_index(
        "ix_memory_rejections_principal",
        "memory_rejections",
        ["tenant_id", "principal_id", "created_at"],
    )
    op.create_table(
        "consolidation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("watermark_before", sa.BigInteger(), nullable=False),
        sa.Column("watermark_after", sa.BigInteger(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("policy_version", sa.Text(), nullable=False),
        sa.Column("candidates_proposed", sa.Integer(), nullable=False),
        sa.Column("committed", sa.Integer(), nullable=False),
        sa.Column("reinforced", sa.Integer(), nullable=False),
        sa.Column("superseded", sa.Integer(), nullable=False),
        sa.Column("rejected", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_consolidation_runs")),
    )
    op.create_table(
        "consolidation_watermarks",
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint(
            "tenant_id", "principal_id", "session_id", name=op.f("pk_consolidation_watermarks")
        ),
    )
    op.create_table(
        "recall_traces",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("principal_id", sa.Text(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("trace", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("operator_fields_expire_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recall_traces")),
    )
    op.create_index("ix_recall_traces_turn", "recall_traces", ["turn_id", "created_at"])
    op.create_index(
        "ix_recall_traces_trace_gin",
        "recall_traces",
        ["trace"],
        postgresql_using="gin",
    )
    op.create_table(
        "knowledge_documents",
        sa.Column("row_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Text(), nullable=False),
        sa.Column("ingested_by_principal_id", sa.Text(), nullable=False),
        sa.Column("visibility", sa.Text(), nullable=False),
        sa.Column("project_scope", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("doc_date", sa.Date(), nullable=True),
        sa.Column("authority", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("chunker_version", sa.Text(), nullable=False),
        sa.Column("superseded_by", sa.Uuid(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sensitivity", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["source_artifact_id"], ["artifacts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["superseded_by"], ["knowledge_documents.row_id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("row_id", name=op.f("pk_knowledge_documents")),
        sa.UniqueConstraint(
            "tenant_id", "document_id", "version", name="uq_knowledge_document_version"
        ),
    )
    op.create_index(
        "ix_knowledge_documents_live",
        "knowledge_documents",
        ["tenant_id", "document_id", "valid_to"],
    )
    op.create_table(
        "knowledge_chunks",
        sa.Column("chunk_id", sa.Text(), nullable=False),
        sa.Column("document_row_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("heading_path", postgresql.JSONB(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("tokens", sa.Integer(), nullable=False),
        sa.Column("contains_instruction_like_text", sa.Boolean(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_row_id"], ["knowledge_documents.row_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("chunk_id", name=op.f("pk_knowledge_chunks")),
        sa.UniqueConstraint(
            "document_row_id", "ordinal", name="uq_knowledge_chunk_document_ordinal"
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_fts",
        "knowledge_chunks",
        [sa.text("to_tsvector('simple', heading_path::text || ' ' || text)")],
        postgresql_using="gin",
    )
    for table in (
        "memories",
        "memory_rejections",
        "consolidation_runs",
        "consolidation_watermarks",
        "recall_traces",
        "knowledge_documents",
    ):
        _tenant_policy(table)
    op.execute("ALTER TABLE knowledge_chunks ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE knowledge_chunks FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY knowledge_chunks_tenant_isolation ON knowledge_chunks "
        "USING (EXISTS (SELECT 1 FROM knowledge_documents d "
        "WHERE d.row_id = knowledge_chunks.document_row_id)) "
        "WITH CHECK (EXISTS (SELECT 1 FROM knowledge_documents d "
        "WHERE d.row_id = knowledge_chunks.document_row_id))"
    )


def downgrade() -> None:
    op.drop_table("knowledge_chunks")
    op.drop_table("knowledge_documents")
    op.drop_table("recall_traces")
    op.drop_table("consolidation_watermarks")
    op.drop_table("consolidation_runs")
    op.drop_table("memory_rejections")
    op.drop_table("memories")
    op.execute("DROP SEQUENCE memory_store_position_seq")
