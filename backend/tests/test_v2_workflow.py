"""D1 graph decisions and durable boundaries, without browser/model calls.

DB tests require the existing explicit ``ZHIDA_TEST_DATABASE_URL`` fixture.
No Redis, RabbitMQ, live recruitment site or paid model is needed here.
"""
from datetime import datetime, timezone
from contextlib import contextmanager
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.backend_v2.messaging import LeaseLost, LeaseStore, QueueStore
from app.backend_v2.repository import Repository, encode, new_id
from app.backend_v2.workflow import (STATE_VERSION, WORKFLOW_VERSION, DurableWorkflowEngine,
                                    _budget_reason, build_graph)
from app.backend_v2.workers import StaleWork


@pytest.mark.parametrize('changes,status,reason', [
    ({}, 'awaiting_browser', 'browser_not_bound'),
    ({'browser_session_id': 'bound'}, 'awaiting_browser', 'recognition_not_enabled'),
    ({'pending_confirmation_count': 2}, 'awaiting_user', 'pending_confirmation'),
    ({'pending_confirmation_count': 2, 'unresolved_action_count': 1},
     'reconciling', 'pending_action_reconciliation'),
    ({'block_reason': 'resume_unavailable'}, 'failed', 'resume_unavailable'),
])
def test_real_graph_decisions_are_bounded_and_never_claim_autofill(changes, status, reason):
    graph = build_graph()
    state = {'pending_confirmation_count': 0, 'unresolved_action_count': 0,
             'browser_session_id': None, 'block_reason': None, **changes}
    result = graph.invoke(state, config={'recursion_limit': 8})
    assert result['status'] == status and result['reason'] == reason
    assert len(result['trace']) == 3
    assert result['trace'][:2] == ['bind_context', 'validate_context']
    assert result['next_node'] == 'bind_context'


@pytest.mark.parametrize('budget,step,nodes,reason', [
    ({}, 1, 3, None), ({'max_advances': 1}, 2, 6, 'workflow_budget_exhausted'),
    ({'max_graph_steps': 2}, 1, 3, 'workflow_budget_exhausted'),
    ({'max_advances': True}, 1, 3, 'invalid_workflow_budget'),
    ({'max_advances': 10000}, 1, 3, 'invalid_workflow_budget'),
    ({'deadline_at': '2020-01-01T00:00:00Z'}, 1, 3, 'workflow_deadline_exceeded'),
    ({'deadline_at': '2030-01-01'}, 1, 3, 'invalid_workflow_budget'),
    ({'deadline_at': 'not-a-date'}, 1, 3, 'invalid_workflow_budget'),
])
def test_budgets_are_bounded_without_repeated_browser_attempts(budget, step, nodes, reason):
    assert _budget_reason(budget, step_no=step, total_node_steps=nodes,
                          now=datetime(2026, 1, 1, tzinfo=timezone.utc)) == reason


def test_ambient_langsmith_tracing_is_disabled(monkeypatch):
    from langsmith.run_helpers import get_tracing_context
    from app.backend_v2 import workflow

    class NoDatabase:
        @contextmanager
        def transaction(self, _):
            yield None

    monkeypatch.setenv('LANGSMITH_TRACING', 'true')
    authority = {'run': {'run_revision': 1, 'status': 'running', 'workflow_version': WORKFLOW_VERSION,
                        'budget': {}, 'resume_revision_id': 'revision', 'browser_session_id': None},
                 'resume_exists': True, 'browser_epoch': None, 'pending_count': 0,
                 'unresolved_count': 0, 'observation_id': None, 'fingerprint': 'synthetic'}
    monkeypatch.setattr(workflow, '_read_authority', lambda *args, **kwargs: authority)
    monkeypatch.setattr(workflow, '_read_head', lambda *args: None)
    engine = DurableWorkflowEngine(NoDatabase())
    invoke = engine.graph.invoke

    def checked(state, config):
        assert get_tracing_context()['enabled'] is False
        assert config['callbacks'] == []
        return invoke(state, config=config)

    monkeypatch.setattr(engine.graph, 'invoke', checked)
    assert engine.advance(user_id='synthetic-user', run_id='synthetic-run', expected_revision=1).state == 'awaiting_browser'


@pytest.fixture
def workflow_run(v2_db):
    user = 'workflow-test-' + uuid4().hex
    repo = Repository(v2_db)
    repo.ensure_user(user)
    selected = repo.save_resume(user, name='Synthetic D1', profile={'name': 'PRIVATE-SYNTHETIC-NAME'})
    run = repo.create_run(user, resume_revision_id=selected['revision_id'],
                         application_url='https://recruit.example.invalid/form', idempotency_key='one')
    with v2_db.transaction(user) as conn:
        conn.execute(text('UPDATE zhida.application_runs SET workflow_version=:v WHERE id=:r'),
                     {'r': run['id'], 'v': WORKFLOW_VERSION})
    return user, run['id'], selected


def commit(db, user, outcome):
    with db.transaction(user) as conn:
        outcome.effects(conn)


def state(db, user, run):
    with db.transaction(user) as conn:
        return dict(conn.execute(text("""SELECT c.* FROM zhida.workflow_heads h
            JOIN zhida.workflow_checkpoints c ON c.id=h.checkpoint_id AND c.user_id=h.user_id
            WHERE h.user_id=:u AND h.run_id=:r"""), {'u': user, 'r': run}).mappings().one())


def add_question(db, user, run, *, question_id=None):
    identity = question_id or new_id()
    with db.transaction(user) as conn:
        conn.execute(text("""INSERT INTO zhida.confirmation_requests
            (id,user_id,run_id,question_revision,label,semantic_key,subject_id,scope_key,reason)
            VALUES(:id,:u,:r,1,'PRIVATE-SYNTHETIC-QUESTION','synthetic.confirmation',:id,'global','missing_fact')"""),
            {'id': identity, 'u': user, 'r': run})
    return identity


def add_unknown_action(db, user, run):
    observation, plan, question, action = [new_id() for _ in range(4)]
    with db.transaction(user) as conn:
        conn.execute(text("""INSERT INTO zhida.form_observations
            VALUES(:id,:u,:r,'synthetic-document',1,'{}','{}')"""), {'id': observation, 'u': user, 'r': run})
        conn.execute(text("""INSERT INTO zhida.questions
            (id,user_id,observation_id,label,section,control_type,evidence_refs)
            VALUES(:id,:u,:o,'Synthetic','Synthetic','text','{}')"""), {'id': question, 'u': user, 'o': observation})
        conn.execute(text("""INSERT INTO zhida.fill_plans
            (id,user_id,run_id,run_revision,observation_id,fact_revision_refs)
            VALUES(:id,:u,:r,1,:o,'[]')"""), {'id': plan, 'u': user, 'r': run, 'o': observation})
        conn.execute(text("""INSERT INTO zhida.action_ledger
            (action_id,user_id,run_id,plan_id,question_id,ordinal,action_type,target_ref,desired_value_ref,status)
            VALUES(:id,:u,:r,:p,:q,0,'set_text','{}','{}','unknown')"""),
            {'id': action, 'u': user, 'r': run, 'p': plan, 'q': question})
    return action


def test_engine_restart_rehydrates_only_committed_head_and_reference_state(v2_db, workflow_run):
    user, run, _ = workflow_run
    engine = DurableWorkflowEngine(v2_db)
    first = engine.advance(user_id=user, run_id=run, expected_revision=1)
    assert first.result['browser_actions_executed'] == 0
    # A computation without effects has no durable checkpoint at all.
    with v2_db.transaction(user) as conn:
        assert conn.execute(text('SELECT count(*) FROM zhida.workflow_checkpoints WHERE run_id=:r'), {'r': run}).scalar() == 0
    restarted = DurableWorkflowEngine(v2_db)
    replay = restarted.advance(user_id=user, run_id=run, expected_revision=1)
    assert replay.result['checkpoint_id'] == first.result['checkpoint_id']
    commit(v2_db, user, replay)
    first_state = state(v2_db, user, run)
    assert first_state['step_no'] == 1
    assert 'PRIVATE-SYNTHETIC-NAME' not in encode(first_state['state'])
    assert 'application_url' not in first_state['state']
    # Even if a future candidate writer inserts an uncommitted headless boundary,
    # loading by MAX(created_at)/MAX(step_no) must never resume it.
    with v2_db.transaction(user) as conn:
        conn.execute(text("""INSERT INTO zhida.workflow_checkpoints
            (id,user_id,run_id,workflow_version,step_no,input_revision,output_revision,state)
            VALUES(:id,:u,:r,:v,999,998,999,CAST(:state AS jsonb))"""),
            {'id': new_id(), 'u': user, 'r': run, 'v': WORKFLOW_VERSION,
             'state': encode({'schema_version': STATE_VERSION, 'total_node_steps': 999999})})
    second = DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=2)
    commit(v2_db, user, second)
    saved = state(v2_db, user, run)
    assert saved['step_no'] == 2 and saved['state']['total_node_steps'] == 6
    assert saved['state']['previous_checkpoint_id'] == first_state['id']
    assert saved['state']['resume_from'] == 'bind_context'


def test_checkpoint_run_event_and_inbox_commit_or_rollback_together(v2_db, workflow_run):
    user, run, _ = workflow_run
    with v2_db.transaction(user) as conn:
        event = conn.execute(text("""SELECT current_event_id FROM zhida.background_jobs
            WHERE user_id=:u AND aggregate_id=:r AND kind='workflow.advance'"""), {'u': user, 'r': run}).scalar_one()
    queue = QueueStore(v2_db)
    claim = queue.claim(event, user_id=user, consumer='synthetic-d1', owner_id='worker-a')
    outcome = DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=1)

    def crash_before_commit(conn):
        outcome.effects(conn)
        raise RuntimeError('synthetic rollback after all graph writes')

    with pytest.raises(RuntimeError):
        queue.complete(claim, state=outcome.state, result=outcome.result, effects=crash_before_commit)
    with v2_db.transaction(user) as conn:
        assert conn.execute(text('SELECT count(*) FROM zhida.workflow_checkpoints WHERE run_id=:r'), {'r': run}).scalar() == 0
        assert conn.execute(text('SELECT run_revision FROM zhida.application_runs WHERE id=:r'), {'r': run}).scalar() == 1
        assert conn.execute(text('SELECT count(*) FROM zhida.run_events WHERE run_id=:r'), {'r': run}).scalar() == 1
        assert conn.execute(text('SELECT status FROM zhida.inbox_events WHERE event_id=:e'), {'e': event}).scalar() == 'processing'
    queue.complete(claim, state=outcome.state, result=outcome.result, effects=outcome.effects)
    assert queue.claim(event, user_id=user, consumer='synthetic-d1', owner_id='worker-b').status == 'completed'
    assert state(v2_db, user, run)['step_no'] == 1
    with pytest.raises(StaleWork):
        DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=1)


def test_pending_questions_remain_waiting_and_resume_revalidates(v2_db, workflow_run):
    user, run, _ = workflow_run
    question = add_question(v2_db, user, run)
    first = DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=1)
    assert first.state == 'awaiting_user'
    commit(v2_db, user, first)
    assert 'PRIVATE-SYNTHETIC-QUESTION' not in encode(state(v2_db, user, run)['state'])
    # Simulate the domain service committing a user response and bumping the run;
    # graph restart must not reuse the old pending count or plan cursor.
    with v2_db.transaction(user) as conn:
        conn.execute(text("UPDATE zhida.confirmation_requests SET status='answered' WHERE id=:id"), {'id': question})
        conn.execute(text("UPDATE zhida.application_runs SET status='queued',run_revision=3 WHERE id=:r"), {'r': run})
    second = DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=3)
    assert second.state == 'awaiting_browser'
    commit(v2_db, user, second)
    assert state(v2_db, user, run)['state']['pending_confirmation_count'] == 0


def test_unknown_action_is_quarantined_not_replayed(v2_db, workflow_run):
    user, run, _ = workflow_run
    action = add_unknown_action(v2_db, user, run)
    outcome = DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=1)
    assert outcome.state == 'reconciling' and outcome.result['browser_actions_executed'] == 0
    commit(v2_db, user, outcome)
    with v2_db.transaction(user) as conn:
        assert conn.execute(text('SELECT status FROM zhida.action_ledger WHERE action_id=:id'), {'id': action}).scalar() == 'unknown'
        assert conn.execute(text('SELECT status FROM zhida.application_runs WHERE id=:id'), {'id': run}).scalar() == 'reconciling'


@pytest.mark.parametrize('change', ['cancel', 'revision', 'question', 'browser_epoch'])
def test_stale_authority_cannot_publish_a_checkpoint(v2_db, workflow_run, change):
    user, run, _ = workflow_run
    if change == 'browser_epoch':
        with v2_db.transaction(user) as conn:
            conn.execute(text('INSERT INTO zhida.devices(id,user_id,name) VALUES(:id,:u,\'Synthetic\')'), {'id': run, 'u': user})
            conn.execute(text("""INSERT INTO zhida.browser_sessions(id,user_id,device_id,driver,binding)
                VALUES(:id,:u,:id,'synthetic','{}')"""), {'id': run, 'u': user})
            conn.execute(text('UPDATE zhida.application_runs SET browser_session_id=:r WHERE id=:r'), {'r': run})
    outcome = DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=1)
    with v2_db.transaction(user) as conn:
        if change == 'cancel':
            conn.execute(text("UPDATE zhida.application_runs SET status='cancelled',run_revision=2 WHERE id=:r"), {'r': run})
        elif change == 'revision':
            conn.execute(text('UPDATE zhida.application_runs SET run_revision=2 WHERE id=:r'), {'r': run})
        elif change == 'browser_epoch':
            conn.execute(text('UPDATE zhida.browser_sessions SET connection_epoch=2 WHERE id=:r'), {'r': run})
    if change == 'question':
        add_question(v2_db, user, run)
    with pytest.raises(StaleWork):
        commit(v2_db, user, outcome)
    with v2_db.transaction(user) as conn:
        assert conn.execute(text('SELECT count(*) FROM zhida.workflow_checkpoints WHERE run_id=:r'), {'r': run}).scalar() == 0


def test_lost_or_expired_execution_lease_cannot_commit(v2_db, workflow_run):
    user, run, _ = workflow_run
    lease = LeaseStore(v2_db).acquire(resource_id=run, user_id=user, owner_id='synthetic-owner')
    context = {'lease_epoch': lease.epoch, 'event_id': 'synthetic-event', 'job_id': 'synthetic-job', 'lease_lost': lambda: False}
    outcome = DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=1, execution_context=context)
    with v2_db.transaction(user) as conn:
        conn.execute(text("UPDATE zhida.run_leases SET expires_at=now()-interval '1 second' WHERE resource_id=:r"), {'r': run})
    with pytest.raises(LeaseLost):
        commit(v2_db, user, outcome)
    with pytest.raises(LeaseLost):
        DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=1,
                                           execution_context={'lease_lost': lambda: True})


def test_deleted_resume_and_exhausted_budget_fail_closed(v2_db, workflow_run):
    user, run, selected = workflow_run
    with v2_db.transaction(user) as conn:
        conn.execute(text('UPDATE zhida.application_runs SET budget=CAST(:budget AS jsonb) WHERE id=:r'),
                     {'r': run, 'budget': encode({'max_advances': 1})})
    first = DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=1)
    commit(v2_db, user, first)
    second = DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=2)
    assert second.state == 'failed' and second.result['reason'] == 'workflow_budget_exhausted'
    with v2_db.transaction(user) as conn:
        conn.execute(text('UPDATE zhida.resumes SET deleted_at=now() WHERE id=:id'), {'id': selected['id']})
    gone = DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=2)
    assert gone.state == 'failed' and gone.result['reason'] == 'resume_unavailable'


def test_version_and_tenant_isolation_fail_closed(v2_db, workflow_run):
    user, run, _ = workflow_run
    other = 'workflow-other-' + uuid4().hex
    Repository(v2_db).ensure_user(other)
    with pytest.raises(StaleWork):
        DurableWorkflowEngine(v2_db).advance(user_id=other, run_id=run, expected_revision=1)
    first = DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=1)
    commit(v2_db, user, first)
    with v2_db.transaction(other) as conn:
        # Explicit tenant scope holds even for a superuser test fixture; separate
        # runtime-role integration tests exercise FORCE RLS without a predicate.
        assert conn.execute(text('SELECT id FROM zhida.workflow_checkpoints WHERE user_id=:u AND run_id=:r'),
                            {'u': other, 'r': run}).first() is None
    with v2_db.transaction(user) as conn:
        conn.execute(text("UPDATE zhida.application_runs SET workflow_version='unrecognized-v100' WHERE id=:r"), {'r': run})
    with pytest.raises(StaleWork):
        DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=2)


def test_checkpoint_force_rls_without_tenant_predicate(v2_db, workflow_run):
    user, run, _ = workflow_run
    outcome = DurableWorkflowEngine(v2_db).advance(user_id=user, run_id=run, expected_revision=1)
    commit(v2_db, user, outcome)
    role = 'zhida_d1_rls_' + uuid4().hex
    tables = ('workflow_checkpoints', 'workflow_heads', 'workflow_step_commits')
    with v2_db.transaction() as conn:
        permitted = conn.execute(text('SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname=current_user')).scalar()
        if not permitted:
            pytest.skip('A temporary non-bypass role requires role-creation permission')
        conn.execute(text(f'CREATE ROLE "{role}" NOLOGIN NOSUPERUSER NOBYPASSRLS'))
        conn.execute(text(f'GRANT USAGE ON SCHEMA zhida TO "{role}"'))
        for table in tables:
            conn.execute(text(f'GRANT SELECT,INSERT ON zhida.{table} TO "{role}"'))
    try:
        for tenant, expected in ((user, 1), ('synthetic-other-tenant', 0), ('', 0)):
            with v2_db.transaction(tenant) as conn:
                conn.execute(text(f'SET LOCAL ROLE "{role}"'))
                assert conn.execute(text('SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user')).scalar() is False
                for table in tables:
                    assert conn.execute(text(f'SELECT count(*) FROM zhida.{table}')).scalar() == expected
        with pytest.raises(DBAPIError, match='row-level security'):
            with v2_db.transaction('synthetic-other-tenant') as conn:
                conn.execute(text(f'SET LOCAL ROLE "{role}"'))
                conn.execute(text("""INSERT INTO zhida.workflow_checkpoints
                    (id,user_id,run_id,workflow_version,step_no,input_revision,output_revision,state)
                    VALUES(:id,:u,:r,:v,2,2,3,'{}')"""),
                    {'id': new_id(), 'u': user, 'r': run, 'v': WORKFLOW_VERSION})
    finally:
        with v2_db.transaction() as conn:
            for table in tables:
                conn.execute(text(f'REVOKE ALL ON zhida.{table} FROM "{role}"'))
            conn.execute(text(f'REVOKE ALL ON SCHEMA zhida FROM "{role}"'))
            conn.execute(text(f'DROP ROLE "{role}"'))
