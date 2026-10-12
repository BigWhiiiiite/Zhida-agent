"""D3 durable local Runner and graph bridge.

Revision ID: 0009_assisted_runtime
Revises: 0008_readonly_analysis
"""
from alembic import op
from app.backend_v2.assisted_schema import SCHEMA_SQL, row_security_sql

revision = '0009_assisted_runtime'
down_revision = '0008_readonly_analysis'
branch_labels = depends_on = None


def upgrade():
    for sql in SCHEMA_SQL + row_security_sql():
        op.execute(sql)


def downgrade():
    op.execute('DROP TABLE zhida.assisted_actions')
    op.execute('DROP TABLE zhida.assisted_rounds')
    # Deliberately refuse destructive checkpoint downgrades after D3 use.
    op.execute("ALTER TABLE zhida.workflow_step_commits DROP CONSTRAINT workflow_step_commits_status_check")
    op.execute("ALTER TABLE zhida.workflow_step_commits ADD CONSTRAINT workflow_step_commits_status_check "
               "CHECK(status IN ('awaiting_user','awaiting_browser','reconciling','failed'))")
