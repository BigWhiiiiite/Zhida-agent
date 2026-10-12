"""PostgreSQL queue/coordination tables; installed only by explicit migrations."""

SCHEMA_SQL = [
    """CREATE TABLE IF NOT EXISTS zhida.background_jobs (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES zhida.users(id),
        kind TEXT NOT NULL CHECK (kind IN ('workflow.advance','document.parse','rag.index','maintenance')),
        aggregate_id TEXT NOT NULL, expected_revision INTEGER NOT NULL DEFAULT 0,
        idempotency_key TEXT NOT NULL, payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        state TEXT NOT NULL DEFAULT 'queued' CHECK (state IN
          ('queued','processing','retry_wait','completed','failed','cancelled','obsolete',
           'awaiting_user','awaiting_browser','ready_for_review')), attempt INTEGER NOT NULL DEFAULT 0,
        current_event_id TEXT NOT NULL, available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        lease_owner TEXT, lease_epoch BIGINT NOT NULL DEFAULT 0, lease_expires_at TIMESTAMPTZ,
        result JSONB, last_error TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE(user_id, kind, idempotency_key), UNIQUE(user_id, id)
    )""",
    "CREATE INDEX IF NOT EXISTS jobs_due_idx ON zhida.background_jobs(state, available_at)",
    """CREATE TABLE IF NOT EXISTS zhida.outbox_events (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL, job_id TEXT NOT NULL,
        kind TEXT NOT NULL, attempt INTEGER NOT NULL, available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        published_at TIMESTAMPTZ, publishing_owner TEXT, publish_lease_expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE(user_id, id),
        FOREIGN KEY(user_id, job_id) REFERENCES zhida.background_jobs(user_id, id)
    )""",
    "CREATE INDEX IF NOT EXISTS outbox_due_idx ON zhida.outbox_events(available_at) WHERE published_at IS NULL",
    """CREATE TABLE IF NOT EXISTS zhida.inbox_events (
        consumer TEXT NOT NULL, event_id TEXT NOT NULL, user_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'processing', owner TEXT, lease_epoch BIGINT NOT NULL DEFAULT 0,
        lease_expires_at TIMESTAMPTZ, completed_at TIMESTAMPTZ,
        PRIMARY KEY(consumer, event_id),
        FOREIGN KEY(user_id, event_id) REFERENCES zhida.outbox_events(user_id, id)
    )""",
    """CREATE TABLE IF NOT EXISTS zhida.run_leases (
        resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES zhida.users(id),
        owner_id TEXT NOT NULL, epoch BIGINT NOT NULL, expires_at TIMESTAMPTZ NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS zhida.runner_leases (
        resource_id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES zhida.users(id),
        owner_id TEXT NOT NULL, epoch BIGINT NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
        needs_reconciliation BOOLEAN NOT NULL DEFAULT false
    )""",
    """CREATE TABLE IF NOT EXISTS zhida.model_calls (
        id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES zhida.users(id), run_id TEXT,
        provider TEXT NOT NULL, model TEXT NOT NULL, request_hash TEXT NOT NULL,
        reserved_tokens INTEGER NOT NULL CHECK (reserved_tokens >= 0), actual_usage JSONB,
        status TEXT NOT NULL DEFAULT 'reserved', started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        finished_at TIMESTAMPTZ, UNIQUE(user_id,id),
        FOREIGN KEY(user_id,run_id) REFERENCES zhida.application_runs(user_id,id)
    )""",
    "CREATE INDEX IF NOT EXISTS model_calls_pending_idx ON zhida.model_calls(user_id, status, started_at)",
]
