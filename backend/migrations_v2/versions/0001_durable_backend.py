"""Initial A-C backend; only run explicitly against a dedicated PostgreSQL DB."""
from alembic import op

revision = "0001_durable_backend"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    from app.backend_v2.schema import SCHEMA_SQL, row_security_sql
    from app.backend_v2.queue_schema import SCHEMA_SQL as QUEUE_SQL
    from app.backend_v2.rag_schema import SCHEMA_SQL as RAG_SQL
    for statement in [*SCHEMA_SQL, *QUEUE_SQL, *RAG_SQL, *row_security_sql()]:
        op.execute(statement)


def downgrade():
    # Do not turn a mistaken downgrade into deletion of every profile and fact.
    raise RuntimeError("V2 downgrade is intentionally blocked. Restore a verified backup into a separate DB.")
