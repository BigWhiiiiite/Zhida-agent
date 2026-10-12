"""PostgreSQL-only RAG storage; schema creation is owned by the V2 migrator."""

SCHEMA_SQL: list[str] = [
    "CREATE SCHEMA IF NOT EXISTS rag",
    "CREATE EXTENSION IF NOT EXISTS vector",
    """CREATE TABLE IF NOT EXISTS rag.knowledge_sources (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES zhida.users(id),
        source_kind TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
        resume_revision_id TEXT, site_scope TEXT,
        revision_no INTEGER NOT NULL DEFAULT 0, current_revision_id TEXT,
        revoked_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(user_id,id),
        FOREIGN KEY(user_id,resume_revision_id) REFERENCES zhida.resume_revisions(user_id,id)
    )""",
    """CREATE TABLE IF NOT EXISTS rag.source_revisions (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, source_id TEXT NOT NULL,
        revision_no INTEGER NOT NULL, content_hash TEXT NOT NULL, records JSONB NOT NULL,
        revoked_at TIMESTAMPTZ, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(user_id,id), UNIQUE(user_id,source_id,id), UNIQUE(source_id,revision_no),
        FOREIGN KEY(user_id,source_id) REFERENCES rag.knowledge_sources(user_id,id)
    )""",
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='rag_source_current_revision_fk'
          AND conrelid='rag.knowledge_sources'::regclass) THEN
          ALTER TABLE rag.knowledge_sources ADD CONSTRAINT rag_source_current_revision_fk
          FOREIGN KEY(user_id,id,current_revision_id)
          REFERENCES rag.source_revisions(user_id,source_id,id) DEFERRABLE INITIALLY DEFERRED;
        END IF;
    END $$""",
    """CREATE TABLE IF NOT EXISTS rag.embedding_profiles (
        id TEXT PRIMARY KEY, model_name TEXT NOT NULL, model_revision TEXT NOT NULL,
        dimension INTEGER NOT NULL CHECK(dimension=512), metric TEXT NOT NULL CHECK(metric='cosine'),
        configuration JSONB NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS rag.index_generations (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, source_id TEXT NOT NULL,
        source_revision_id TEXT NOT NULL,
        embedding_profile_id TEXT NOT NULL REFERENCES rag.embedding_profiles(id),
        chunker_version TEXT NOT NULL, tokenizer_version TEXT NOT NULL,
        state TEXT NOT NULL CHECK(state IN ('building','lexical_ready','ready','failed','obsolete')),
        expected_count INTEGER NOT NULL DEFAULT 0, ready_count INTEGER NOT NULL DEFAULT 0,
        build_token TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(user_id,id), UNIQUE(user_id,id,source_revision_id),
        UNIQUE(user_id,source_id,source_revision_id,embedding_profile_id,id),
        UNIQUE(user_id,source_revision_id,embedding_profile_id,chunker_version,tokenizer_version),
        FOREIGN KEY(user_id,source_id,source_revision_id)
          REFERENCES rag.source_revisions(user_id,source_id,id)
    )""",
    """CREATE TABLE IF NOT EXISTS rag.evidence_chunks (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, generation_id TEXT NOT NULL,
        source_revision_id TEXT NOT NULL, entity_id TEXT NOT NULL, entity_type TEXT NOT NULL,
        ordinal INTEGER NOT NULL, text TEXT NOT NULL, embedding_text TEXT NOT NULL,
        parent_context TEXT NOT NULL, source_path TEXT NOT NULL,
        source_span JSONB NOT NULL, text_hash TEXT NOT NULL, lexemes TSVECTOR NOT NULL,
        UNIQUE(user_id,id), UNIQUE(generation_id,ordinal),
        UNIQUE(user_id,id,generation_id,source_revision_id,entity_id),
        FOREIGN KEY(user_id,generation_id,source_revision_id)
          REFERENCES rag.index_generations(user_id,id,source_revision_id)
    )""",
    """CREATE TABLE IF NOT EXISTS rag.chunk_vectors_bge512_v1 (
        chunk_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, generation_id TEXT NOT NULL,
        source_revision_id TEXT NOT NULL, entity_id TEXT NOT NULL, embedding VECTOR(512) NOT NULL,
        FOREIGN KEY(user_id,chunk_id,generation_id,source_revision_id,entity_id)
          REFERENCES rag.evidence_chunks(user_id,id,generation_id,source_revision_id,entity_id)
          ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS rag.active_index_heads (
        user_id TEXT NOT NULL, source_id TEXT NOT NULL, retrieval_profile TEXT NOT NULL,
        desired_source_revision_id TEXT NOT NULL,
        lexical_generation_id TEXT, vector_generation_id TEXT, revision INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(user_id,source_id,retrieval_profile),
        FOREIGN KEY(user_id,source_id,desired_source_revision_id)
          REFERENCES rag.source_revisions(user_id,source_id,id),
        FOREIGN KEY(user_id,source_id,desired_source_revision_id,retrieval_profile,lexical_generation_id)
          REFERENCES rag.index_generations(user_id,source_id,source_revision_id,embedding_profile_id,id),
        FOREIGN KEY(user_id,source_id,desired_source_revision_id,retrieval_profile,vector_generation_id)
          REFERENCES rag.index_generations(user_id,source_id,source_revision_id,embedding_profile_id,id)
    )""",
    "CREATE INDEX IF NOT EXISTS rag_chunks_lexemes_gin ON rag.evidence_chunks USING gin(lexemes)",
    "CREATE INDEX IF NOT EXISTS rag_chunks_scope_idx ON rag.evidence_chunks(user_id,generation_id,entity_id)",
    "CREATE INDEX IF NOT EXISTS rag_vectors_scope_idx ON rag.chunk_vectors_bge512_v1(user_id,generation_id,entity_id)",
    "CREATE INDEX IF NOT EXISTS rag_source_scope_idx ON rag.knowledge_sources(user_id,resume_revision_id,site_scope)",
    """CREATE INDEX IF NOT EXISTS rag_vectors_hnsw ON rag.chunk_vectors_bge512_v1
        USING hnsw (embedding vector_cosine_ops) WITH (m=16,ef_construction=64)""",
]

# Every private table has an explicit owner; service queries also filter it.
# Migrators own the tables, whereas runtime roles must not have BYPASSRLS.
for _table in (
    "knowledge_sources", "source_revisions", "index_generations", "evidence_chunks",
    "chunk_vectors_bge512_v1", "active_index_heads",
):
    SCHEMA_SQL.extend([
        f"ALTER TABLE rag.{_table} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE rag.{_table} FORCE ROW LEVEL SECURITY",
        f"""DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE schemaname='rag'
                           AND tablename='{_table}' AND policyname='owner_scope') THEN
              CREATE POLICY owner_scope ON rag.{_table}
              USING (user_id=current_setting('zhida.user_id',true))
              WITH CHECK (user_id=current_setting('zhida.user_id',true));
            END IF;
        END $$""",
    ])
