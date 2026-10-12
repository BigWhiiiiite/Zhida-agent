"""Durable, tenant-scoped D1 LangGraph continuation boundaries."""
from alembic import op

revision = '0007_workflow_runtime'
down_revision = '0006_workbench_projection'
branch_labels = depends_on = None


def upgrade():
    from app.backend_v2.workflow_schema import SCHEMA_SQL, row_security_sql
    for statement in [*SCHEMA_SQL, *row_security_sql()]:
        op.execute(statement)
    op.execute('ALTER TABLE zhida.background_jobs DROP CONSTRAINT background_jobs_state_check')
    op.execute("""ALTER TABLE zhida.background_jobs ADD CONSTRAINT background_jobs_state_check
        CHECK(state IN ('queued','processing','retry_wait','completed','failed','cancelled',
                       'obsolete','awaiting_user','awaiting_browser','ready_for_review','reconciling'))""")
    # Native bootstrap already installs default grants. This also covers a runtime
    # role provisioned before those defaults, without requiring that role in CI.
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM pg_roles WHERE rolname='zhida_runtime') THEN
            GRANT SELECT,INSERT,UPDATE,DELETE ON zhida.workflow_checkpoints,
                zhida.workflow_heads,zhida.workflow_step_commits TO zhida_runtime;
        END IF;
    END $$""")


def downgrade():
    raise RuntimeError('Workflow history is retained; restore a verified backup into a separate DB.')
