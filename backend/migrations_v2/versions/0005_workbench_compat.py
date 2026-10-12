"""Preserve all existing workbench records while cutting storage over to PostgreSQL."""
from alembic import op
from app.backend_v2.workbench_schema import schema_sql

revision = "0005_workbench_compat"
down_revision = "0004_memory_entity_identity"
branch_labels = depends_on = None


def upgrade():
    for statement in schema_sql():
        op.execute(statement)


def downgrade():
    raise RuntimeError("Workbench data must not be dropped by an automatic downgrade")
