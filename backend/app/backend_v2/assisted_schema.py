"""D3 local Runner receipts. Browser values live only in tenant-protected rows."""

SCHEMA_SQL = [
    """ALTER TABLE zhida.workflow_step_commits DROP CONSTRAINT workflow_step_commits_status_check""",
    """ALTER TABLE zhida.workflow_step_commits ADD CONSTRAINT workflow_step_commits_status_check
        CHECK(status IN ('awaiting_user','awaiting_browser','reconciling','failed','ready_for_review'))""",
    """CREATE TABLE zhida.assisted_rounds (
        id text PRIMARY KEY, user_id text NOT NULL, run_id text NOT NULL,
        legacy_session_id text NOT NULL, legacy_resume_id text NOT NULL,
        request_hash text NOT NULL, context_hash text NOT NULL,
        process_id text NOT NULL, request jsonb NOT NULL,
        status text NOT NULL DEFAULT 'queued'
          CHECK(status IN ('queued','authorized','running','finished','interrupted')),
        cancel_requested boolean NOT NULL DEFAULT false,
        progress jsonb NOT NULL DEFAULT '{}', result jsonb,
        created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(user_id,id), FOREIGN KEY(user_id,run_id) REFERENCES zhida.application_runs(user_id,id))""",
    """CREATE UNIQUE INDEX assisted_one_running_device ON zhida.assisted_rounds(user_id)
        WHERE status IN ('queued','authorized','running')""",
    """CREATE TABLE zhida.assisted_actions (
        id text PRIMARY KEY, user_id text NOT NULL, round_id text NOT NULL,
        command_type text NOT NULL CHECK(command_type IN
          ('set_text','select_option','set_date','check_option','add_record','upload_file')),
        command_hash text NOT NULL, target jsonb NOT NULL, desired jsonb NOT NULL,
        status text NOT NULL CHECK(status IN ('dispatched','verified','failed','unknown')),
        evidence jsonb NOT NULL DEFAULT '{}', created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(user_id,id), FOREIGN KEY(user_id,round_id) REFERENCES zhida.assisted_rounds(user_id,id))""",
    "CREATE INDEX assisted_actions_unresolved ON zhida.assisted_actions(user_id,status,round_id)",
    "CREATE INDEX assisted_round_run ON zhida.assisted_rounds(user_id,run_id,created_at)",
]


def row_security_sql():
    return [sql for name in ('assisted_rounds','assisted_actions') for sql in (
        f'ALTER TABLE zhida.{name} ENABLE ROW LEVEL SECURITY',
        f'ALTER TABLE zhida.{name} FORCE ROW LEVEL SECURITY',
        f"CREATE POLICY tenant_scope ON zhida.{name} USING "
        "(user_id=current_setting('zhida.user_id',true)) WITH CHECK "
        "(user_id=current_setting('zhida.user_id',true))")]
