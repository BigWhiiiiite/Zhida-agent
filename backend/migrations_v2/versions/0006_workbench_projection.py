"""One-way, transactional workbench projections; legacy IDs remain UI IDs."""
from alembic import op

revision = "0006_workbench_projection"
down_revision = "0005_workbench_compat"
branch_labels = depends_on = None


def upgrade():
    op.execute("ALTER TABLE zhida.resumes ADD COLUMN deleted_at timestamptz")
    op.execute("""CREATE TABLE workbench.projections (
        user_id text NOT NULL REFERENCES zhida.users(id), kind text NOT NULL,
        legacy_id text NOT NULL, core_id text NOT NULL, source_hash text NOT NULL,
        payload jsonb NOT NULL DEFAULT '{}', deleted_at timestamptz,
        PRIMARY KEY(user_id,kind,legacy_id))""")
    op.execute("ALTER TABLE workbench.projections ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE workbench.projections FORCE ROW LEVEL SECURITY")
    op.execute("""CREATE POLICY tenant_isolation ON workbench.projections
        USING (user_id=current_setting('zhida.user_id',true))
        WITH CHECK (user_id=current_setting('zhida.user_id',true))""")


def downgrade():
    raise RuntimeError("Projection history is retained; rollback uses the original SQLite snapshot")
