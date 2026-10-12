"""D1 application-level graph checkpoints; no credentials or resume bodies.

This is deliberately separate from LangGraph's per-superstep PostgresSaver.
One bounded, side-effect-free graph invocation commits its JSON continuation
boundary in the *same transaction* as the run/event and worker Inbox completion.
There are no independently persisted candidate checkpoints to accidentally resume.
"""

SCHEMA_SQL = [
    """CREATE TABLE zhida.workflow_checkpoints (
        id text PRIMARY KEY, user_id text NOT NULL, run_id text NOT NULL,
        workflow_version text NOT NULL, step_no integer NOT NULL CHECK(step_no > 0),
        input_revision integer NOT NULL CHECK(input_revision > 0),
        output_revision integer NOT NULL CHECK(output_revision = input_revision + 1),
        state jsonb NOT NULL CHECK(jsonb_typeof(state) = 'object'),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(user_id,run_id,workflow_version,step_no),
        UNIQUE(user_id,run_id,workflow_version,id,step_no,output_revision),
        FOREIGN KEY(user_id,run_id) REFERENCES zhida.application_runs(user_id,id))""",
    """CREATE TABLE zhida.workflow_heads (
        user_id text NOT NULL, run_id text NOT NULL, workflow_version text NOT NULL,
        checkpoint_id text NOT NULL, step_no integer NOT NULL,
        output_revision integer NOT NULL, updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY(user_id,run_id,workflow_version),
        FOREIGN KEY(user_id,run_id,workflow_version,checkpoint_id,step_no,output_revision)
        REFERENCES zhida.workflow_checkpoints(user_id,run_id,workflow_version,id,step_no,output_revision))""",
    """CREATE TABLE zhida.workflow_step_commits (
        id text PRIMARY KEY, user_id text NOT NULL, run_id text NOT NULL,
        workflow_version text NOT NULL, input_revision integer NOT NULL,
        output_revision integer NOT NULL, step_no integer NOT NULL,
        checkpoint_id text NOT NULL, lease_epoch bigint, event_id text, job_id text,
        status text NOT NULL CHECK(status IN ('awaiting_user','awaiting_browser','reconciling','failed')),
        reason text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE(user_id,run_id,workflow_version,input_revision),
        FOREIGN KEY(user_id,run_id,workflow_version,checkpoint_id,step_no,output_revision)
        REFERENCES zhida.workflow_checkpoints(user_id,run_id,workflow_version,id,step_no,output_revision))""",
    "CREATE INDEX workflow_steps_run_order ON zhida.workflow_step_commits(user_id,run_id,step_no)",
]


def row_security_sql() -> list[str]:
    statements = []
    for name in ('workflow_checkpoints', 'workflow_heads', 'workflow_step_commits'):
        statements.extend([
            f'ALTER TABLE zhida.{name} ENABLE ROW LEVEL SECURITY',
            f'ALTER TABLE zhida.{name} FORCE ROW LEVEL SECURITY',
            f"CREATE POLICY tenant_scope ON zhida.{name} USING "
            "(user_id = current_setting('zhida.user_id', true)) WITH CHECK "
            "(user_id = current_setting('zhida.user_id', true))",
        ])
    return statements
