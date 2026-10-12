"""Lossless private archive for explicit offline legacy snapshot imports."""
from alembic import op

revision = "0003_legacy_archive"
down_revision = "0002_document_artifacts"
branch_labels = depends_on = None


def upgrade():
    op.execute("""CREATE TABLE zhida.legacy_records (
        source_hash text NOT NULL, table_name text NOT NULL, record_id text NOT NULL,
        user_id text NOT NULL REFERENCES zhida.users(id), payload jsonb NOT NULL,
        mapped_id text, confirmation text NOT NULL DEFAULT 'unconfirmed',
        PRIMARY KEY(source_hash,table_name,record_id))""")
    op.execute("ALTER TABLE zhida.legacy_records ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE zhida.legacy_records FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_scope ON zhida.legacy_records
        USING(user_id=current_setting('zhida.user_id',true))
        WITH CHECK(user_id=current_setting('zhida.user_id',true))""")


def downgrade():
    raise RuntimeError("Legacy archive requires a verified backup before removal")
