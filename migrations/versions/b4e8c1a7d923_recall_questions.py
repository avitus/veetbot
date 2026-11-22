"""Add the owner's recall question set and its history (ADR-0339).

Revision ID: b4e8c1a7d923
Revises: a7d3f9c2e514
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b4e8c1a7d923"
down_revision: str | Sequence[str] | None = "a7d3f9c2e514"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "CREATE TABLE recall_questions (  tenant_id TEXT NOT NULL,   principal_id TEXT NOT NULL, "
        "id UUID NOT NULL,   key TEXT NOT NULL,   split TEXT NOT NULL,   status TEXT NOT NULL, "
        "content JSONB NOT NULL,   revision INTEGER NOT NULL,   latest_result JSONB, "
        "created_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "updated_at TIMESTAMP WITH TIME ZONE NOT NULL,   CONSTRAINT pk_recall_questions "
        "PRIMARY KEY (tenant_id, principal_id, id),   CONSTRAINT uq_recall_questions_key "
        "UNIQUE (tenant_id, principal_id, key),   CONSTRAINT ck_recall_questions_split "
        "CHECK (split IN ('development', 'holdout')),   CONSTRAINT ck_recall_questions_status "
        "CHECK (status IN ('active', 'retired')),   CONSTRAINT ck_recall_questions_content "
        "CHECK (jsonb_typeof(content) = 'object'),   CONSTRAINT ck_recall_questions_revision "
        "CHECK (revision >= 1) )"
    )
    op.execute(
        "CREATE TABLE recall_question_revisions (  tenant_id TEXT NOT NULL, "
        "principal_id TEXT NOT NULL,   question_id UUID NOT NULL,   revision INTEGER NOT NULL, "
        "status TEXT NOT NULL,   content JSONB NOT NULL, "
        "changed_at TIMESTAMP WITH TIME ZONE NOT NULL, "
        "CONSTRAINT pk_recall_question_revisions PRIMARY KEY (tenant_id, principal_id, "
        "question_id, revision), "
        "CONSTRAINT fk_recall_question_revisions_tenant_id_recall_questions FOREIGN KEY(tenant_id, "
        "principal_id, question_id) REFERENCES recall_questions (tenant_id, principal_id, id) "
        "ON DELETE CASCADE,   CONSTRAINT ck_recall_question_revisions_content "
        "CHECK (jsonb_typeof(content) = 'object') )"
    )
    op.execute("ALTER TABLE recall_questions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE recall_questions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY recall_questions_tenant_isolation ON recall_questions USING (tenant_id = "
        "current_setting('agent_core.tenant_id', true)) WITH CHECK (tenant_id = "
        "current_setting('agent_core.tenant_id', true))"
    )
    op.execute("ALTER TABLE recall_question_revisions ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE recall_question_revisions FORCE ROW LEVEL SECURITY")
    op.execute(
        "CREATE POLICY recall_question_revisions_tenant_isolation ON recall_question_revisions "
        "USING (tenant_id = current_setting('agent_core.tenant_id', true)) WITH CHECK "
        "(tenant_id = current_setting('agent_core.tenant_id', true))"
    )


def downgrade() -> None:
    """Refuse while questions exist: they are owner-written and live nowhere else."""
    op.execute("SET LOCAL row_security = off")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM recall_questions) THEN "
        "RAISE EXCEPTION 'Recall questions must be exported and erased before downgrade'; "
        "END IF; END $$"
    )
    op.drop_table("recall_question_revisions")
    op.drop_table("recall_questions")
