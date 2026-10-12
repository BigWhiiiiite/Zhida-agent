"""Owner-scoped read-only form analysis, separate from executable fill plans."""
from alembic import op

revision = '0008_readonly_analysis'
down_revision = '0007_workflow_runtime'
branch_labels = depends_on = None


def upgrade():
    op.execute("""CREATE TABLE zhida.form_analysis_reports (
        id text PRIMARY KEY, user_id text NOT NULL, run_id text NOT NULL,
        observation_id text NOT NULL, checkpoint_id text NOT NULL,
        run_revision integer NOT NULL, report jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(user_id,run_id,run_revision),
        FOREIGN KEY(user_id,run_id) REFERENCES zhida.application_runs(user_id,id) ON DELETE CASCADE,
        FOREIGN KEY(user_id,observation_id,run_id)
            REFERENCES zhida.form_observations(user_id,id,run_id) ON DELETE CASCADE)""")
    op.execute('ALTER TABLE zhida.form_analysis_reports ENABLE ROW LEVEL SECURITY')
    op.execute('ALTER TABLE zhida.form_analysis_reports FORCE ROW LEVEL SECURITY')
    op.execute("""CREATE POLICY tenant_scope ON zhida.form_analysis_reports
        USING (user_id=current_setting('zhida.user_id',true))
        WITH CHECK (user_id=current_setting('zhida.user_id',true))""")
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='zhida_runtime') THEN
            GRANT SELECT,INSERT,UPDATE,DELETE ON zhida.form_analysis_reports TO zhida_runtime;
        END IF;
    END $$""")


def downgrade():
    raise RuntimeError('Analysis history is retained; use a verified backup for rollback.')
