"""Frozen PostgreSQL compatibility schema for the existing workbench API.

JSON and ISO timestamps intentionally remain TEXT here: the old API performs
byte-for-byte optimistic comparisons. Native V2 tables hold typed projections.
"""

WORKBENCH_TABLES = {
    "users": """id TEXT PRIMARY KEY, email TEXT NOT NULL, display_name TEXT NOT NULL DEFAULT '',
        password_hash TEXT NOT NULL, failed_attempts INTEGER NOT NULL DEFAULT 0, locked_until TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL""",
    "sessions": """token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL""",
    "resumes": """id TEXT PRIMARY KEY, filename TEXT NOT NULL, label TEXT NOT NULL,
        profile_json TEXT NOT NULL, parser TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        stored_filename TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'needs_review',
        language TEXT NOT NULL DEFAULT '未识别', tags_json TEXT NOT NULL DEFAULT '[]',
        target_role TEXT NOT NULL DEFAULT '', is_default INTEGER NOT NULL DEFAULT 0,
        file_size BIGINT NOT NULL DEFAULT 0, content_hash TEXT NOT NULL DEFAULT '', raw_text TEXT NOT NULL DEFAULT '',
        evidence_json TEXT NOT NULL DEFAULT '[]', error_message TEXT NOT NULL DEFAULT '', user_id TEXT NOT NULL""",
    "confirmed_resume_facts": """id TEXT PRIMARY KEY, user_id TEXT NOT NULL, resume_id TEXT NOT NULL,
        section TEXT NOT NULL, record_key TEXT NOT NULL, attribute TEXT NOT NULL,
        audit_json TEXT NOT NULL, created_at TEXT NOT NULL""",
    "candidate_profiles": """id TEXT PRIMARY KEY, profile_json TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, user_id TEXT NOT NULL UNIQUE""",
    "conflicts": """id TEXT PRIMARY KEY, field_path TEXT NOT NULL,
        current_value_json TEXT NOT NULL, incoming_value_json TEXT NOT NULL, resume_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', resolution_json TEXT, created_at TEXT NOT NULL, user_id TEXT NOT NULL""",
    "application_queue": """id TEXT PRIMARY KEY, user_id TEXT NOT NULL, job_id TEXT NOT NULL,
        resume_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'planned',
        notes TEXT NOT NULL DEFAULT '', application_id TEXT NOT NULL DEFAULT '',
        status_changed_at TEXT NOT NULL DEFAULT '', submitted_at TEXT,
        job_snapshot_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        assistance_started_at TEXT, confirmation_pending INTEGER NOT NULL DEFAULT 0, UNIQUE(user_id,job_id)""",
    "job_verifications": """job_id TEXT PRIMARY KEY, status TEXT NOT NULL, checked_at TEXT NOT NULL,
        official_url TEXT NOT NULL, final_url TEXT NOT NULL DEFAULT '', http_status INTEGER,
        page_title TEXT NOT NULL DEFAULT '', evidence_json TEXT NOT NULL DEFAULT '[]', message TEXT NOT NULL DEFAULT ''""",
    "discovered_jobs": """job_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, job_json TEXT NOT NULL,
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL""",
    "job_source_runs": """source_id TEXT PRIMARY KEY, status TEXT NOT NULL, started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL, jobs_seen INTEGER NOT NULL DEFAULT 0, total_available INTEGER,
        partial INTEGER NOT NULL DEFAULT 1, message TEXT NOT NULL DEFAULT ''""",
    "job_sources": """id TEXT PRIMARY KEY, user_id TEXT NOT NULL, config_json TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL""",
    "rag_index_state": """user_id TEXT PRIMARY KEY, profile_hash TEXT NOT NULL, model TEXT NOT NULL,
        indexed_at TEXT NOT NULL""",
    "rag_evidence": """user_id TEXT NOT NULL, chunk_id TEXT NOT NULL, source_kind TEXT NOT NULL,
        source_title TEXT NOT NULL, source_path TEXT NOT NULL, quote TEXT NOT NULL, embedding_text TEXT NOT NULL,
        strength DOUBLE PRECISION NOT NULL, vector_json TEXT NOT NULL, PRIMARY KEY(user_id,chunk_id)""",
    "rag_evidence_fts": """user_id TEXT NOT NULL, chunk_id TEXT NOT NULL, quote TEXT NOT NULL,
        document tsvector GENERATED ALWAYS AS (to_tsvector('simple',quote)) STORED,
        PRIMARY KEY(user_id,chunk_id)""",
    "application_agent_runs": """run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
        status TEXT NOT NULL, stage TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL,
        events_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE(user_id,session_id)""",
    "chat_conversations": """id TEXT PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL,
        conversation_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL""",
    "application_knowledge": """id TEXT PRIMARY KEY, user_id TEXT NOT NULL, site_scope TEXT NOT NULL,
        record_json TEXT NOT NULL, embedding_text TEXT NOT NULL, vector_model TEXT NOT NULL DEFAULT '',
        vector_json TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL""",
}

AUTH_TABLES = {"users", "sessions"}
CACHE_TABLES = {"rag_index_state", "rag_evidence", "rag_evidence_fts"}
SHARED_TABLES = {"job_verifications", "discovered_jobs", "job_source_runs"}
PRIVATE_TABLES = set(WORKBENCH_TABLES) - AUTH_TABLES - SHARED_TABLES
IMPORT_TABLES = tuple(name for name in WORKBENCH_TABLES if name not in CACHE_TABLES | {"sessions"})


def schema_sql() -> list[str]:
    statements = ["CREATE SCHEMA IF NOT EXISTS workbench"]
    statements += [f"CREATE TABLE workbench.{name} ({definition})"
                   for name, definition in WORKBENCH_TABLES.items()]
    statements += [
        "CREATE UNIQUE INDEX wb_users_email ON workbench.users(lower(email))",
        "CREATE INDEX wb_sessions_owner ON workbench.sessions(user_id,expires_at)",
        "CREATE INDEX wb_resumes_owner ON workbench.resumes(user_id,updated_at)",
        "CREATE INDEX wb_confirmed_facts_owner ON workbench.confirmed_resume_facts(user_id,resume_id,created_at)",
        "CREATE INDEX wb_conflicts_owner ON workbench.conflicts(user_id,status)",
        "CREATE INDEX wb_sources_owner ON workbench.job_sources(user_id,updated_at)",
        "CREATE INDEX wb_discovered_source ON workbench.discovered_jobs(source_id,last_seen_at)",
        "CREATE INDEX wb_chat_owner ON workbench.chat_conversations(user_id,updated_at)",
        "CREATE INDEX wb_knowledge_owner ON workbench.application_knowledge(user_id,site_scope)",
        "CREATE INDEX wb_fts_document ON workbench.rag_evidence_fts USING gin(document)",
        """CREATE TABLE workbench.import_receipts (
            source_hash TEXT PRIMARY KEY, imported_at timestamptz NOT NULL DEFAULT now(), report jsonb NOT NULL)""",
    ]
    for table in sorted(PRIVATE_TABLES):
        statements.extend([
            f"ALTER TABLE workbench.{table} ADD CONSTRAINT wb_owner "
            "FOREIGN KEY(user_id) REFERENCES zhida.users(id) DEFERRABLE INITIALLY DEFERRED",
            f"ALTER TABLE workbench.{table} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE workbench.{table} FORCE ROW LEVEL SECURITY",
            f"CREATE POLICY tenant_scope ON workbench.{table} USING "
            "(user_id <> '' AND user_id = current_setting('zhida.user_id',true)) WITH CHECK "
            "(user_id <> '' AND user_id = current_setting('zhida.user_id',true))",
        ])
    return statements
