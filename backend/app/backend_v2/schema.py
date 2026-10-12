"""Frozen initial business DDL. Future changes require new Alembic revisions."""

SCHEMA_SQL = [
    "CREATE SCHEMA IF NOT EXISTS zhida",
    "CREATE SCHEMA IF NOT EXISTS rag",
    """CREATE TABLE zhida.users (
        id text PRIMARY KEY, email text UNIQUE, password_hash text,
        display_name text NOT NULL DEFAULT '', created_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE zhida.auth_sessions (
        token_hash text PRIMARY KEY, user_id text NOT NULL REFERENCES zhida.users(id),
        expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now())""",
    """CREATE TABLE zhida.files (
        id text PRIMARY KEY, user_id text NOT NULL REFERENCES zhida.users(id),
        storage_key text NOT NULL, content_hash text NOT NULL, mime text NOT NULL,
        size bigint NOT NULL CHECK(size >= 0), created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(user_id,id), UNIQUE(user_id,storage_key))""",
    """CREATE TABLE zhida.resumes (
        id text PRIMARY KEY, user_id text NOT NULL REFERENCES zhida.users(id),
        name text NOT NULL, current_revision_id text,
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(user_id,id))""",
    """CREATE TABLE zhida.resume_revisions (
        id text PRIMARY KEY, user_id text NOT NULL, resume_id text NOT NULL,
        revision_no integer NOT NULL CHECK(revision_no > 0), file_id text,
        profile jsonb NOT NULL, content_hash text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(user_id,id), UNIQUE(user_id,id,resume_id), UNIQUE(resume_id,revision_no),
        FOREIGN KEY(user_id,resume_id) REFERENCES zhida.resumes(user_id,id),
        FOREIGN KEY(user_id,file_id) REFERENCES zhida.files(user_id,id))""",
    """ALTER TABLE zhida.resumes ADD CONSTRAINT resume_current_owned
        FOREIGN KEY(user_id,current_revision_id,id)
        REFERENCES zhida.resume_revisions(user_id,id,resume_id) DEFERRABLE INITIALLY DEFERRED""",
    """CREATE TABLE zhida.experience_entities (
        id text PRIMARY KEY, user_id text NOT NULL, resume_id text NOT NULL,
        entity_type text NOT NULL CHECK(entity_type IN ('education','project','internship','skill')),
        UNIQUE(user_id,id), UNIQUE(user_id,id,resume_id),
        FOREIGN KEY(user_id,resume_id) REFERENCES zhida.resumes(user_id,id))""",
    """CREATE TABLE zhida.experience_versions (
        id text PRIMARY KEY, user_id text NOT NULL, resume_id text NOT NULL,
        entity_id text NOT NULL, resume_revision_id text NOT NULL,
        education_level text, content jsonb NOT NULL, source_span text NOT NULL,
        UNIQUE(entity_id,resume_revision_id),
        FOREIGN KEY(user_id,entity_id,resume_id) REFERENCES zhida.experience_entities(user_id,id,resume_id),
        FOREIGN KEY(user_id,resume_revision_id,resume_id) REFERENCES zhida.resume_revisions(user_id,id,resume_id))""",
    """CREATE TABLE zhida.profile_facts (
        id text PRIMARY KEY, user_id text NOT NULL REFERENCES zhida.users(id),
        subject_id text NOT NULL, semantic_key text NOT NULL, scope_key text NOT NULL,
        current_revision_id text, UNIQUE(user_id,id),
        UNIQUE(user_id,subject_id,semantic_key,scope_key))""",
    """CREATE TABLE zhida.fact_revisions (
        id text PRIMARY KEY, user_id text NOT NULL, fact_id text NOT NULL,
        revision_no integer NOT NULL, value jsonb NOT NULL, source jsonb NOT NULL,
        confirmation text NOT NULL CHECK(confirmation IN ('confirmed','unconfirmed','revoked')),
        valid_until timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(user_id,id), UNIQUE(user_id,id,fact_id), UNIQUE(fact_id,revision_no),
        FOREIGN KEY(user_id,fact_id) REFERENCES zhida.profile_facts(user_id,id))""",
    """ALTER TABLE zhida.profile_facts ADD CONSTRAINT current_fact_owned
        FOREIGN KEY(user_id,current_revision_id,id) REFERENCES zhida.fact_revisions(user_id,id,fact_id)
        DEFERRABLE INITIALLY DEFERRED""",
    """CREATE TABLE zhida.answer_memories (
        id text PRIMARY KEY, user_id text NOT NULL, fact_id text NOT NULL,
        fact_revision_id text NOT NULL, question_semantics text NOT NULL, scope_key text NOT NULL,
        UNIQUE(user_id,question_semantics,scope_key),
        FOREIGN KEY(user_id,fact_revision_id,fact_id) REFERENCES zhida.fact_revisions(user_id,id,fact_id))""",
    """CREATE TABLE zhida.site_rules (
        id text PRIMARY KEY, user_id text NOT NULL REFERENCES zhida.users(id),
        site text NOT NULL, form_fingerprint text NOT NULL, revision integer NOT NULL,
        mapping jsonb NOT NULL, verified_evidence jsonb NOT NULL,
        status text NOT NULL CHECK(status IN ('pending','verified','revoked')),
        UNIQUE(user_id,site,form_fingerprint,revision))""",
    """CREATE TABLE zhida.devices (
        id text PRIMARY KEY, user_id text NOT NULL REFERENCES zhida.users(id),
        name text NOT NULL, token_hash text, UNIQUE(user_id,id))""",
    """CREATE TABLE zhida.browser_sessions (
        id text PRIMARY KEY, user_id text NOT NULL, device_id text NOT NULL,
        driver text NOT NULL, binding jsonb NOT NULL, connection_epoch bigint NOT NULL DEFAULT 1,
        capabilities jsonb NOT NULL DEFAULT '{}', UNIQUE(user_id,id),
        FOREIGN KEY(user_id,device_id) REFERENCES zhida.devices(user_id,id))""",
    """CREATE TABLE zhida.application_runs (
        id text PRIMARY KEY, user_id text NOT NULL REFERENCES zhida.users(id),
        resume_revision_id text NOT NULL, browser_session_id text,
        application_url text NOT NULL, status text NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued','running','awaiting_user','awaiting_browser','retry_wait',
                        'reconciling','ready_for_review','failed','cancelled')),
        run_revision integer NOT NULL DEFAULT 1, event_seq bigint NOT NULL DEFAULT 0,
        workflow_version text NOT NULL DEFAULT 'foundation-v1',
        idempotency_key text NOT NULL, request_hash text NOT NULL,
        payload jsonb NOT NULL DEFAULT '{}', budget jsonb NOT NULL DEFAULT '{}',
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(user_id,id), UNIQUE(user_id,idempotency_key),
        FOREIGN KEY(user_id,resume_revision_id) REFERENCES zhida.resume_revisions(user_id,id),
        FOREIGN KEY(user_id,browser_session_id) REFERENCES zhida.browser_sessions(user_id,id))""",
    """CREATE UNIQUE INDEX one_active_run_per_user ON zhida.application_runs(user_id)
        WHERE status IN ('queued','running','awaiting_user','awaiting_browser','retry_wait','reconciling')""",
    "CREATE INDEX runs_user_created ON zhida.application_runs(user_id,created_at DESC)",
    "CREATE INDEX runs_status_updated ON zhida.application_runs(status,updated_at)",
    """CREATE TABLE zhida.form_observations (
        id text PRIMARY KEY, user_id text NOT NULL, run_id text NOT NULL,
        document_id text NOT NULL, observation_revision integer NOT NULL,
        coverage jsonb NOT NULL, evidence jsonb NOT NULL,
        UNIQUE(user_id,id), UNIQUE(user_id,id,run_id), UNIQUE(run_id,observation_revision),
        FOREIGN KEY(user_id,run_id) REFERENCES zhida.application_runs(user_id,id))""",
    """CREATE TABLE zhida.questions (
        id text PRIMARY KEY, user_id text NOT NULL, observation_id text NOT NULL,
        label text NOT NULL, section text NOT NULL, entity_binding text,
        control_type text NOT NULL, options jsonb NOT NULL DEFAULT '[]', evidence_refs jsonb NOT NULL,
        UNIQUE(user_id,id),
        FOREIGN KEY(user_id,observation_id) REFERENCES zhida.form_observations(user_id,id))""",
    """CREATE TABLE zhida.fill_plans (
        id text PRIMARY KEY, user_id text NOT NULL, run_id text NOT NULL,
        run_revision integer NOT NULL, observation_id text NOT NULL,
        fact_revision_refs jsonb NOT NULL, status text NOT NULL DEFAULT 'planned',
        UNIQUE(user_id,id), UNIQUE(user_id,id,run_id),
        FOREIGN KEY(user_id,run_id) REFERENCES zhida.application_runs(user_id,id),
        FOREIGN KEY(user_id,observation_id,run_id) REFERENCES zhida.form_observations(user_id,id,run_id))""",
    """CREATE TABLE zhida.action_ledger (
        action_id text PRIMARY KEY, user_id text NOT NULL, run_id text NOT NULL,
        plan_id text NOT NULL, question_id text NOT NULL, ordinal integer NOT NULL,
        action_type text NOT NULL, target_ref jsonb NOT NULL, desired_value_ref jsonb NOT NULL,
        lease_epoch bigint, status text NOT NULL DEFAULT 'planned'
        CHECK(status IN ('planned','dispatched','verified','failed','unknown','invalidated')),
        before_ref jsonb, after_ref jsonb, error_class text,
        created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(plan_id,ordinal),
        FOREIGN KEY(user_id,plan_id,run_id) REFERENCES zhida.fill_plans(user_id,id,run_id),
        FOREIGN KEY(user_id,question_id) REFERENCES zhida.questions(user_id,id))""",
    "CREATE INDEX actions_run_state ON zhida.action_ledger(run_id,status)",
    """CREATE TABLE zhida.confirmation_requests (
        id text PRIMARY KEY, user_id text NOT NULL, run_id text NOT NULL,
        question_id text, question_revision integer NOT NULL, label text NOT NULL,
        semantic_key text NOT NULL, subject_id text NOT NULL, scope_key text NOT NULL,
        options jsonb NOT NULL DEFAULT '[]', reason text NOT NULL,
        status text NOT NULL DEFAULT 'pending', answer_fact_revision_id text,
        UNIQUE(user_id,id),
        FOREIGN KEY(user_id,run_id) REFERENCES zhida.application_runs(user_id,id),
        FOREIGN KEY(user_id,question_id) REFERENCES zhida.questions(user_id,id),
        FOREIGN KEY(user_id,answer_fact_revision_id) REFERENCES zhida.fact_revisions(user_id,id))""",
    """CREATE TABLE zhida.run_events (
        run_id text NOT NULL, user_id text NOT NULL, seq bigint NOT NULL,
        event_type text NOT NULL, payload jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(run_id,seq), FOREIGN KEY(user_id,run_id) REFERENCES zhida.application_runs(user_id,id))""",
    """CREATE TABLE zhida.import_receipts (
        source_hash text PRIMARY KEY, imported_at timestamptz NOT NULL DEFAULT now(), report jsonb NOT NULL)""",
]

TENANT_TABLES = ["files", "resumes", "resume_revisions", "experience_entities", "experience_versions",
                 "profile_facts", "fact_revisions", "answer_memories", "site_rules", "devices",
                 "browser_sessions", "application_runs", "form_observations", "questions", "fill_plans",
                 "action_ledger", "confirmation_requests", "run_events"]


def row_security_sql() -> list[str]:
    statements = []
    for name in TENANT_TABLES:
        statements.extend([
            f"ALTER TABLE zhida.{name} ENABLE ROW LEVEL SECURITY",
            f"ALTER TABLE zhida.{name} FORCE ROW LEVEL SECURITY",
            f"CREATE POLICY tenant_scope ON zhida.{name} USING "
            "(user_id = current_setting('zhida.user_id', true)) WITH CHECK "
            "(user_id = current_setting('zhida.user_id', true))",
        ])
    return statements
