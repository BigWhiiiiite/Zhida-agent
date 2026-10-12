"""Versioned, review-required document extraction artifacts."""
from alembic import op

revision = "0002_document_artifacts"
down_revision = "0001_durable_backend"
branch_labels = depends_on = None


def upgrade():
    op.execute("""CREATE TABLE zhida.parsed_documents (
        id text PRIMARY KEY, user_id text NOT NULL, file_id text NOT NULL,
        parser_version text NOT NULL, blocks jsonb NOT NULL, warnings jsonb NOT NULL,
        status text NOT NULL CHECK(status IN ('needs_review','needs_ocr')),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(user_id,file_id,parser_version), UNIQUE(user_id,id),
        FOREIGN KEY(user_id,file_id) REFERENCES zhida.files(user_id,id))""")
    op.execute("ALTER TABLE zhida.parsed_documents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE zhida.parsed_documents FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_scope ON zhida.parsed_documents
        USING(user_id=current_setting('zhida.user_id',true))
        WITH CHECK(user_id=current_setting('zhida.user_id',true))""")


def downgrade():
    raise RuntimeError("Document artifacts require an explicit backup and retention decision")
