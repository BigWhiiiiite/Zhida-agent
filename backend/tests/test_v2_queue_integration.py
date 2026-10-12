"""Real PostgreSQL transactional/recovery tests. v2_db enforces a test database."""
import uuid
from contextlib import contextmanager

import pytest
from sqlalchemy import text

from app.backend_v2.messaging import (LeaseLost, LeaseStore, OutboxPublisher, QueueStore,
                                      enqueue_job)
from app.backend_v2.workers import RetryableWork, WorkResult, WorkerDispatcher


@pytest.fixture
def queue_user(v2_db):
    user = 'queue-test-' + uuid.uuid4().hex
    with v2_db.transaction() as conn:
        conn.execute(text('INSERT INTO zhida.users(id) VALUES (:id)'), {'id': user})
    yield user
    with v2_db.transaction(user) as conn:
        for table in ('form_analysis_reports', 'confirmation_requests',
                      'workflow_heads', 'workflow_step_commits', 'workflow_checkpoints',
                      'inbox_events', 'outbox_events', 'model_calls', 'background_jobs',
                      'run_leases', 'runner_leases', 'action_ledger', 'fill_plans', 'questions',
                      'form_observations', 'run_events', 'application_runs', 'resume_revisions', 'resumes'):
            conn.execute(text('DELETE FROM zhida.' + table + ' WHERE user_id=:user'), {'user': user})
        conn.execute(text('DELETE FROM zhida.users WHERE id=:user'), {'user': user})


def create_job(db, user, *, key='one', kind='document.parse', aggregate='document'):
    with db.transaction(user) as conn:
        return enqueue_job(conn, user_id=user, kind=kind, aggregate_id=aggregate,
                           expected_revision=1, idempotency_key=key)


def claim_job(db, user, ref, owner='worker-a'):
    return QueueStore(db).claim(ref.event_id, user_id=user, consumer='test-consumer', owner_id=owner)


def expire_fixture_job(db, user, ref):
    """Make only this fixture first in the real scheduler's bounded scan.

    The reusable test database may contain thousands of older queued fixtures.
    A global recovery count >=1 says nothing about whether OUR job was scanned.
    Keep the production recovery query intact, but give this fixture a unique
    first priority and recover just one row; do not drain other tests' queues.
    """
    with db.transaction(user) as conn:
        conn.execute(text("""UPDATE zhida.background_jobs
            SET lease_expires_at=now()-interval '1 second',updated_at='-infinity'::timestamptz
            WHERE id=:id AND user_id=:user"""), {'id': ref.job_id, 'user': user})


def test_outbox_rolls_back_with_business_transaction(v2_db, queue_user):
    with pytest.raises(RuntimeError):
        with v2_db.transaction(queue_user) as conn:
            enqueue_job(conn, user_id=queue_user, kind='document.parse', aggregate_id='doc',
                        idempotency_key='rolled-back')
            raise RuntimeError('transaction aborted')
    with v2_db.transaction(queue_user) as conn:
        assert conn.execute(text('SELECT count(*) FROM zhida.background_jobs WHERE user_id=:user'),
                            {'user': queue_user}).scalar() == 0


def test_idempotency_checks_payload_and_tenant_scope(v2_db, queue_user):
    first = create_job(v2_db, queue_user)
    assert create_job(v2_db, queue_user) == first
    with pytest.raises(ValueError):
        create_job(v2_db, queue_user, aggregate='changed')
    assert QueueStore(v2_db).claim(first.event_id, user_id='another-user',
                                  consumer='test', owner_id='bad').status == 'missing'


def test_duplicate_claim_does_not_skip_retry_event(v2_db, queue_user):
    ref = create_job(v2_db, queue_user)
    queue = QueueStore(v2_db)
    claim = claim_job(v2_db, queue_user, ref)
    assert claim.status == 'claimed'
    assert claim_job(v2_db, queue_user, ref, 'worker-b').status == 'busy'
    assert queue.retry(claim, reason='transient', delay_seconds=0) == 'retry_wait'
    assert claim_job(v2_db, queue_user, ref).status == 'completed'
    with v2_db.transaction(queue_user) as conn:
        next_event = conn.execute(text('SELECT current_event_id FROM zhida.background_jobs WHERE id=:id'),
                                  {'id': ref.job_id}).scalar()
    assert next_event != ref.event_id
    next_claim = queue.claim(next_event, user_id=queue_user, consumer='test-consumer', owner_id='worker-b')
    assert next_claim.status == 'claimed' and next_claim.job.attempt == 1
    queue.complete(next_claim, state='awaiting_user', result={'question_count': 1})
    assert queue.claim(next_event, user_id=queue_user, consumer='test-consumer', owner_id='worker-c').status == 'completed'


def test_completion_effects_and_inbox_are_one_transaction(v2_db, queue_user):
    ref = create_job(v2_db, queue_user)
    claim = claim_job(v2_db, queue_user, ref)

    def fail(conn):
        conn.execute(text("UPDATE zhida.users SET display_name='should rollback' WHERE id=:id"),
                     {'id': queue_user})
        raise RuntimeError('business mutation failed')
    with pytest.raises(RuntimeError):
        QueueStore(v2_db).complete(claim, effects=fail)
    with v2_db.transaction(queue_user) as conn:
        assert conn.execute(text('SELECT display_name FROM zhida.users WHERE id=:id'), {'id': queue_user}).scalar() == ''
        assert conn.execute(text('SELECT status FROM zhida.inbox_events WHERE event_id=:id'),
                            {'id': ref.event_id}).scalar() == 'processing'


def test_expired_worker_cannot_commit_and_recovery_reclaims(v2_db, queue_user):
    ref = create_job(v2_db, queue_user)
    queue = QueueStore(v2_db)
    stale = claim_job(v2_db, queue_user, ref)
    expire_fixture_job(v2_db, queue_user, ref)
    assert queue.recover_expired(limit=1) == 1
    newer = claim_job(v2_db, queue_user, ref, 'new-worker')
    assert newer.status == 'claimed' and newer.epoch > stale.epoch
    with pytest.raises(LeaseLost):
        queue.complete(stale)
    queue.complete(newer)


def test_publisher_marks_only_confirmed_messages(v2_db, queue_user):
    ref = create_job(v2_db, queue_user)
    with v2_db.transaction(queue_user) as conn:
        conn.execute(text("""UPDATE zhida.outbox_events SET available_at='-infinity'::timestamptz
            WHERE id=:id AND user_id=:user"""), {'id': ref.event_id, 'user': queue_user})
    failed_attempts = []
    def unavailable(event):
        failed_attempts.append(event['id'])
        raise OSError('broker unavailable')
    assert OutboxPublisher(v2_db, unavailable).publish_batch(limit=1) == 0
    assert failed_attempts == [ref.event_id]
    with v2_db.transaction(queue_user) as conn:
        first = conn.execute(text('SELECT published_at,publishing_owner,publish_lease_expires_at>now() AS leased '
                                  'FROM zhida.outbox_events WHERE id=:id AND user_id=:user'),
                             {'id': ref.event_id, 'user': queue_user}).mappings().one()
        assert first['published_at'] is None and first['publishing_owner'] and first['leased']
        conn.execute(text("UPDATE zhida.outbox_events SET publish_lease_expires_at=now()-interval '1 second' WHERE id=:id"),
                     {'id': ref.event_id})
    received = []
    assert OutboxPublisher(v2_db, received.append).publish_batch(limit=1) == 1
    assert [event['id'] for event in received] == [ref.event_id]
    with v2_db.transaction(queue_user) as conn:
        assert conn.execute(text('SELECT published_at FROM zhida.outbox_events WHERE id=:id'),
                            {'id': ref.event_id}).scalar() is not None


def test_fenced_browser_lease_quarantines_unknown_outcomes(v2_db, queue_user):
    leases = LeaseStore(v2_db)
    old = leases.acquire(resource_id='test-device-' + queue_user, user_id=queue_user, owner_id='old', browser=True)
    assert old
    assert leases.acquire(resource_id=old.resource_id, user_id=queue_user, owner_id='new', browser=True) is None
    assert leases.release(old, unknown=True)
    assert leases.acquire(resource_id=old.resource_id, user_id=queue_user, owner_id='new', browser=True) is None


def test_missing_parser_fails_instead_of_reporting_success(v2_db, queue_user):
    ref = create_job(v2_db, queue_user)
    result = WorkerDispatcher(v2_db).dispatch(event_id=ref.event_id, user_id=queue_user)
    assert result['status'] == 'failed'


def test_retry_result_and_new_outbox_are_atomic(v2_db, queue_user):
    ref = create_job(v2_db, queue_user)
    claim = claim_job(v2_db, queue_user, ref)
    def fail(conn, state):
        assert state == 'retry_wait'
        raise RuntimeError('cannot persist business retry')
    with pytest.raises(RuntimeError):
        QueueStore(v2_db).retry(claim, reason='transient', delay_seconds=0, effects=fail)
    with v2_db.transaction(queue_user) as conn:
        row = conn.execute(text('SELECT current_event_id,attempt,state FROM zhida.background_jobs WHERE id=:id'),
                           {'id': ref.job_id}).mappings().one()
        assert row['current_event_id'] == ref.event_id and row['attempt'] == 0 and row['state'] == 'processing'


def test_recovery_single_leader_skips_while_another_scheduler_owns_lock(v2_db, queue_user):
    ref = create_job(v2_db, queue_user)
    claim_job(v2_db, queue_user, ref)
    expire_fixture_job(v2_db, queue_user, ref)
    with v2_db.transaction() as first:
        assert first.execute(text('SELECT pg_try_advisory_xact_lock(15000, 2)')).scalar()
        assert QueueStore(v2_db).recover_expired(limit=1) == 0
    assert QueueStore(v2_db).recover_expired(limit=1) == 1
    with v2_db.transaction(queue_user) as conn:
        assert conn.execute(text('SELECT state FROM zhida.background_jobs WHERE id=:id'),
                            {'id': ref.job_id}).scalar_one() == 'queued'


@pytest.mark.parametrize('unknown_action', [False, True])
def test_worker_recovery_honors_rls_with_non_superuser_role(v2_db, queue_user, unknown_action):
    role = 'zhida_queue_test_worker'
    with v2_db.transaction() as conn:
        conn.execute(text("""DO $$ BEGIN
          IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='zhida_queue_test_worker') THEN
            CREATE ROLE zhida_queue_test_worker NOLOGIN NOSUPERUSER NOBYPASSRLS;
          END IF;
        END $$"""))
        conn.execute(text('GRANT USAGE ON SCHEMA zhida TO zhida_queue_test_worker'))
        conn.execute(text('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA zhida TO zhida_queue_test_worker'))
    class RuntimeDatabase:
        @contextmanager
        def transaction(self, user_id=None):
            with v2_db.transaction(user_id) as conn:
                conn.execute(text('SET LOCAL ROLE zhida_queue_test_worker'))
                yield conn
    runtime = RuntimeDatabase()
    run_id, resume_id, revision_id = (uuid.uuid4().hex for _ in range(3))
    with runtime.transaction(queue_user) as conn:
        assert conn.execute(text("SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname=current_user")).scalar() is False
        conn.execute(text('INSERT INTO zhida.resumes(id,user_id,name) VALUES(:id,:user,\'fixture\')'),
                     {'id': resume_id, 'user': queue_user})
        conn.execute(text("""INSERT INTO zhida.resume_revisions
            (id,user_id,resume_id,revision_no,profile,content_hash) VALUES(:id,:user,:resume,1,'{}','fixture')"""),
                     {'id': revision_id, 'user': queue_user, 'resume': resume_id})
        conn.execute(text("""INSERT INTO zhida.application_runs
            (id,user_id,resume_revision_id,application_url,status,idempotency_key,request_hash)
            VALUES(:id,:user,:resume,'https://example.org/fixture','running',:id,'fixture')"""),
                     {'id': run_id, 'user': queue_user, 'resume': revision_id})
        if unknown_action:
            observation, question, plan, action = (uuid.uuid4().hex for _ in range(4))
            params = {'user': queue_user, 'run': run_id, 'observation': observation,
                      'question': question, 'plan': plan, 'action': action}
            conn.execute(text("""INSERT INTO zhida.form_observations
                (id,user_id,run_id,document_id,observation_revision,coverage,evidence)
                VALUES(:observation,:user,:run,'fixture-document',1,'{}','{}')"""), params)
            conn.execute(text("""INSERT INTO zhida.questions
                (id,user_id,observation_id,label,section,control_type,evidence_refs)
                VALUES(:question,:user,:observation,'fixture-question','fixture-section','text','[]')"""), params)
            conn.execute(text("""INSERT INTO zhida.fill_plans
                (id,user_id,run_id,run_revision,observation_id,fact_revision_refs)
                VALUES(:plan,:user,:run,1,:observation,'[]')"""), params)
            conn.execute(text("""INSERT INTO zhida.action_ledger
                (action_id,user_id,run_id,plan_id,question_id,ordinal,action_type,target_ref,desired_value_ref,status)
                VALUES(:action,:user,:run,:plan,:question,0,'set_text','{}','{}','unknown')"""), params)
    ref = create_job(runtime, queue_user, kind='workflow.advance', aggregate=run_id)
    claim_job(runtime, queue_user, ref)
    with runtime.transaction() as conn:
        assert conn.execute(text('SELECT count(*) FROM zhida.application_runs WHERE id=:id'), {'id': run_id}).scalar() == 0
    expire_fixture_job(runtime, queue_user, ref)
    assert QueueStore(runtime).recover_expired(limit=1) == 1
    with runtime.transaction(queue_user) as conn:
        assert conn.execute(text('SELECT status FROM zhida.application_runs WHERE id=:id'), {'id': run_id}).scalar() == (
            'reconciling' if unknown_action else 'queued')
    handlers = {'workflow.advance': lambda _: pytest.fail('unknown action must not invoke workflow')} if unknown_action else {}
    result = WorkerDispatcher(runtime, handlers=handlers).dispatch(event_id=ref.event_id, user_id=queue_user)
    assert result['status'] == 'awaiting_browser'
    with runtime.transaction(queue_user) as conn:
        assert conn.execute(text('SELECT status FROM zhida.application_runs WHERE id=:id'), {'id': run_id}).scalar() == 'awaiting_browser'
        if unknown_action:
            value = conn.execute(text('SELECT result FROM zhida.background_jobs WHERE id=:id'), {'id': ref.job_id}).scalar()
            assert value['reason'] == 'pending_action_reconciliation'
