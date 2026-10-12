"""The same question wording must not overwrite another subject's memory."""
from alembic import op

revision = "0004_memory_entity_identity"
down_revision = "0003_legacy_archive"
branch_labels = depends_on = None


def upgrade():
    op.execute("""ALTER TABLE zhida.answer_memories
        DROP CONSTRAINT answer_memories_user_id_question_semantics_scope_key_key""")
    op.execute("""ALTER TABLE zhida.answer_memories ADD CONSTRAINT memory_fact_question_scope
        UNIQUE(user_id,fact_id,question_semantics,scope_key)""")


def downgrade():
    raise RuntimeError("Cannot collapse entity-specific memories into a wording-only identity")
