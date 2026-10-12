"""D1 through the real dispatcher and opt-in isolated RabbitMQ/Celery path.

No browser/model is invoked. Every durable row belongs to a fresh synthetic
user. The broker case publishes only its own Outbox event to a uniquely named
test queue/exchange in an explicitly validated ``zhida_test*`` vhost.
"""
import faulthandler
import os
import sys
import threading
import time
from urllib.parse import unquote, urlparse
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.backend_v2.api import create_app
from app.backend_v2.db import Database
from app.backend_v2.redis_control import RateDecision
from app.backend_v2.repository import Repository, new_id
from app.backend_v2.settings import Settings
from app.backend_v2.workflow import DurableWorkflowEngine, WORKFLOW_VERSION
from app.backend_v2.workers import (ExecutionContext, WorkerDispatcher, confirmed_publish,
                                    create_celery_app)


@pytest.fixture
def worker_run(v2_db):
    user = 'workflow-worker-' + uuid4().hex
    repo = Repository(v2_db)
    repo.ensure_user(user)
    selected = repo.save_resume(user, name='Synthetic worker resume', profile={})
    run = repo.create_run(user, resume_revision_id=selected['revision_id'],
        application_url='https://recruit.synthetic.invalid/form', idempotency_key='worker-run-request',
        workflow_version=WORKFLOW_VERSION)
    return user, run


def event_for(db, user, run_id, revision):
    with db.transaction(user) as conn:
        return dict(conn.execute(text("""SELECT e.id,e.user_id,e.job_id,e.kind,e.attempt
            FROM zhida.outbox_events e JOIN zhida.background_jobs j
              ON j.id=e.job_id AND j.user_id=e.user_id AND j.current_event_id=e.id
            WHERE j.user_id=:u AND j.aggregate_id=:r AND j.expected_revision=:revision
              AND j.kind='workflow.advance'"""),
            {'u': user, 'r': run_id, 'revision': revision}).mappings().one())


def checkpoints(db, user, run_id):
    with db.transaction(user) as conn:
        return [dict(row) for row in conn.execute(text("""SELECT c.*,s.lease_epoch,s.event_id,s.job_id
            FROM zhida.workflow_checkpoints c JOIN zhida.workflow_step_commits s
              ON s.user_id=c.user_id AND s.checkpoint_id=c.id
            WHERE c.user_id=:u AND c.run_id=:r ORDER BY c.step_no"""),
            {'u': user, 'r': run_id}).mappings()]


def assert_no_checkpoint(db, user, run_id):
    with db.transaction(user) as conn:
        for table in ('workflow_checkpoints', 'workflow_heads', 'workflow_step_commits'):
            assert conn.execute(text(f'SELECT count(*) FROM zhida.{table} WHERE user_id=:u AND run_id=:r'),
                                {'u': user, 'r': run_id}).scalar_one() == 0


def add_unknown_action(db, user, run_id):
    observation, question, plan, action = [new_id() for _ in range(4)]
    with db.transaction(user) as conn:
        conn.execute(text("""INSERT INTO zhida.form_observations
            (id,user_id,run_id,document_id,observation_revision,coverage,evidence)
            VALUES(:id,:u,:r,'synthetic-worker-document',1,'{}','{}')"""),
            {'id': observation, 'u': user, 'r': run_id})
        conn.execute(text("""INSERT INTO zhida.questions
            (id,user_id,observation_id,label,section,control_type,evidence_refs)
            VALUES(:id,:u,:o,'Synthetic','Synthetic','text','{}')"""),
            {'id': question, 'u': user, 'o': observation})
        conn.execute(text("""INSERT INTO zhida.fill_plans
            (id,user_id,run_id,run_revision,observation_id,fact_revision_refs)
            VALUES(:id,:u,:r,1,:o,'[]')"""), {'id': plan, 'u': user, 'r': run_id, 'o': observation})
        conn.execute(text("""INSERT INTO zhida.action_ledger
            (action_id,user_id,run_id,plan_id,question_id,ordinal,action_type,target_ref,desired_value_ref,status)
            VALUES(:id,:u,:r,:p,:q,0,'fill_text','{}','{}','unknown')"""),
            {'id': action, 'u': user, 'r': run_id, 'p': plan, 'q': question})
    return action


def test_dispatcher_prefers_explicit_engine_and_passes_live_fencing_context(v2_db, worker_run):
    user, run = worker_run
    event = event_for(v2_db, user, run['id'], 1)
    obsolete_calls, contexts = [], []

    def obsolete_handler(job):
        obsolete_calls.append(job)
        raise AssertionError('An obsolete workflow handler must not shadow the engine')

    class ObservedEngine(DurableWorkflowEngine):
        def advance(self, *, execution_context, **kwargs):
            assert isinstance(execution_context, ExecutionContext)
            assert execution_context.lease_epoch == 1
            assert execution_context.event_id == event['id']
            assert execution_context.job_id == event['job_id']
            assert execution_context.lease_lost() is False
            with self.db.transaction(user) as conn:
                assert conn.execute(text("""SELECT epoch FROM zhida.run_leases
                    WHERE user_id=:u AND resource_id=:r AND expires_at>now()"""),
                    {'u': user, 'r': run['id']}).scalar_one() == execution_context.lease_epoch
            contexts.append(execution_context)
            return super().advance(execution_context=execution_context, **kwargs)

    dispatcher = WorkerDispatcher(v2_db, handlers={'workflow.advance': obsolete_handler},
                                  engine=ObservedEngine(v2_db))
    assert dispatcher.dispatch(event_id=event['id'], user_id=user)['status'] == 'awaiting_browser'
    assert not obsolete_calls and len(contexts) == 1
    saved = checkpoints(v2_db, user, run['id'])
    assert len(saved) == 1 and saved[0]['lease_epoch'] == 1
    assert saved[0]['event_id'] == event['id'] and saved[0]['job_id'] == event['job_id']
    assert saved[0]['state']['trace'] == ['bind_context', 'validate_context', 'wait_for_browser']
    # Broker redelivery sees a completed Inbox, not a second graph execution.
    assert dispatcher.dispatch(event_id=event['id'], user_id=user)['status'] == 'completed'
    assert len(contexts) == 1 and len(checkpoints(v2_db, user, run['id'])) == 1
    with v2_db.transaction(user) as conn:
        assert conn.execute(text("""SELECT count(*) FROM zhida.inbox_events
            WHERE user_id=:u AND event_id=:e AND status='completed'"""),
            {'u': user, 'e': event['id']}).scalar_one() == 1


def test_unknown_action_reaches_graph_reconciliation_and_commits_checkpoint(v2_db, worker_run):
    user, run = worker_run
    action = add_unknown_action(v2_db, user, run['id'])
    event = event_for(v2_db, user, run['id'], 1)
    dispatcher = WorkerDispatcher(v2_db, engine=DurableWorkflowEngine(v2_db))
    assert dispatcher.dispatch(event_id=event['id'], user_id=user)['status'] == 'reconciling'
    saved = checkpoints(v2_db, user, run['id'])
    assert len(saved) == 1 and saved[0]['state']['trace'][-1] == 'reconcile'
    assert saved[0]['state']['unresolved_action_count'] == 1
    assert saved[0]['state']['reason'] == 'pending_action_reconciliation'
    with v2_db.transaction(user) as conn:
        assert conn.execute(text('SELECT status FROM zhida.action_ledger WHERE action_id=:a AND user_id=:u'),
                            {'a': action, 'u': user}).scalar_one() == 'unknown'
        job = conn.execute(text('SELECT state,result FROM zhida.background_jobs WHERE id=:id AND user_id=:u'),
                           {'id': event['job_id'], 'u': user}).mappings().one()
        assert job['state'] == 'reconciling' and job['result']['browser_actions_executed'] == 0
    assert dispatcher.dispatch(event_id=event['id'], user_id=user)['status'] == 'completed'
    assert len(checkpoints(v2_db, user, run['id'])) == 1


def test_new_worker_pool_resumes_from_committed_boundary_after_domain_wakeup(v2_db, worker_run):
    user, run = worker_run
    event = event_for(v2_db, user, run['id'], 1)
    first = WorkerDispatcher(v2_db, engine=DurableWorkflowEngine(v2_db))
    assert first.dispatch(event_id=event['id'], user_id=user)['status'] == 'awaiting_browser'
    first_state = checkpoints(v2_db, user, run['id'])[0]
    assert first_state['output_revision'] == 2
    fresh_db = Database(v2_db.engine.url.render_as_string(hide_password=False))
    try:
        resumed = Repository(fresh_db).resume_run(user, run['id'], expected_revision=2)
        assert resumed['run_revision'] == 3
        wakeup = event_for(fresh_db, user, run['id'], 3)
        second = WorkerDispatcher(fresh_db, engine=DurableWorkflowEngine(fresh_db))
        assert second.dispatch(event_id=wakeup['id'], user_id=user)['status'] == 'awaiting_browser'
        saved = checkpoints(fresh_db, user, run['id'])
        assert [row['step_no'] for row in saved] == [1, 2]
        assert [row['output_revision'] for row in saved] == [2, 4]
        assert saved[-1]['state']['previous_checkpoint_id'] == first_state['id']
        assert saved[-1]['state']['resume_from'] == 'bind_context'
        assert saved[-1]['lease_epoch'] == 2
        assert Repository(fresh_db).get_run(user, run['id'])['run_revision'] == 4
    finally:
        fresh_db.close()


@pytest.mark.parametrize('change,expected', [('cancel', 'obsolete'), ('lease_expired', 'lease_lost'),
                                           ('lease_replaced', 'lease_lost')])
def test_change_after_graph_computation_prevents_worker_checkpoint_commit(v2_db, worker_run, change, expected):
    user, run = worker_run
    event = event_for(v2_db, user, run['id'], 1)

    class ChangedEngine(DurableWorkflowEngine):
        def advance(self, **kwargs):
            outcome = super().advance(**kwargs)
            # Simulate a cancellation or stale worker after pure computation but
            # before QueueStore.complete runs the fenced effects transaction.
            if change == 'cancel':
                Repository(self.db).cancel_run(user, run['id'], expected_revision=1)
            else:
                with self.db.transaction(user) as conn:
                    if change == 'lease_expired':
                        conn.execute(text("""UPDATE zhida.run_leases
                            SET expires_at=now()-interval '1 second' WHERE user_id=:u AND resource_id=:r"""),
                            {'u': user, 'r': run['id']})
                    else:
                        conn.execute(text("""UPDATE zhida.run_leases SET epoch=epoch+1,owner_id='synthetic-successor'
                            WHERE user_id=:u AND resource_id=:r"""), {'u': user, 'r': run['id']})
            return outcome

    dispatcher = WorkerDispatcher(v2_db, engine=ChangedEngine(v2_db))
    assert dispatcher.dispatch(event_id=event['id'], user_id=user)['status'] == expected
    assert_no_checkpoint(v2_db, user, run['id'])
    if change == 'cancel':
        assert Repository(v2_db).get_run(user, run['id'])['status'] == 'cancelled'
    with v2_db.transaction(user) as conn:
        state = conn.execute(text('SELECT status FROM zhida.inbox_events WHERE user_id=:u AND event_id=:e'),
                             {'u': user, 'e': event['id']}).scalar_one()
        assert state == ('completed' if change == 'cancel' else 'processing')


class AllowedControl:
    def rate_limit(self, **kwargs):
        return RateDecision(True, 10, 0)


def test_native_api_outbox_real_celery_factory_reaches_langgraph(v2_db, monkeypatch):
    broker = os.getenv('ZHIDA_TEST_BROKER_URL', '')
    if not broker:
        pytest.skip('Set ZHIDA_TEST_BROKER_URL for a dedicated RabbitMQ test vhost')
    parsed = urlparse(broker)
    if parsed.scheme not in {'amqp', 'amqps'} or not unquote(parsed.path).lstrip('/').startswith('zhida_test'):
        pytest.fail('D1 broker test requires a dedicated zhida_test* RabbitMQ vhost')
    from celery.contrib.testing.worker import TestWorkController, start_worker
    from celery.signals import task_postrun
    from kombu import Exchange, Queue

    monkeypatch.setenv('ZHIDA_V2_WORKFLOW_ENABLED', 'true')
    database_url = v2_db.engine.url.render_as_string(hide_password=False)
    cfg = Settings(database_url=database_url, cookie_secure=False, workflow_enabled=True)
    obsolete_calls = []

    def obsolete_handler(job):
        obsolete_calls.append(job)
        raise AssertionError('Configured LangGraph must own workflow.advance')

    # No engine_factory override: this is the actual environment-enabled
    # production factory selecting DurableWorkflowEngine in its worker runtime.
    celery = create_celery_app(database_url=database_url, broker_url=broker,
                              handlers={'workflow.advance': obsolete_handler})
    name = 'zhida.test.d1.' + uuid4().hex
    # Match production durability. Recent RabbitMQ versions reject transient
    # non-exclusive queues; do not weaken broker policy for a test fixture.
    isolated_exchange = Exchange(name, type='direct', durable=True, auto_delete=False)
    isolated_queue = Queue(name, exchange=isolated_exchange, routing_key='workflow.advance',
                           alias='workflow.advance', durable=True, auto_delete=False)
    # Register the actual physical name and Kombu's supported logical alias.
    # Assigning queues['workflow.advance'] = Queue(other_name) alone leaves
    # Celery bootsteps unable to resolve that physical name consistently.
    celery.conf.task_queues = (isolated_queue,)
    celery.conf.task_routes = {'zhida.v2.workflow.advance': {'queue': name, 'routing_key': 'workflow.advance'}}
    celery.amqp.queues = celery.amqp.Queues((isolated_queue,))
    assert celery.amqp.queues[name] is celery.amqp.queues['workflow.advance']

    class BoundedWorkerController(TestWorkController):
        def ensure_started(self):
            if self._on_started.wait(20):
                return
            # Celery's testing helper otherwise waits forever when consumer
            # bootsteps repeatedly fail. Emit actionable thread evidence once.
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            shutdown = threading.Thread(target=self.terminate, daemon=True)
            shutdown.start()
            shutdown.join(timeout=3)
            raise TimeoutError('Synthetic D1 Celery worker did not start within 20 seconds')

    deliveries, target = [], {}

    def finished(sender=None, kwargs=None, retval=None, **unused):
        if kwargs and kwargs.get('event_id') == target.get('event_id'):
            deliveries.append(retval)

    workflow_task = celery.tasks['zhida.v2.workflow.advance']
    task_postrun.connect(finished, sender=workflow_task, weak=False)
    try:
        with TestClient(create_app(cfg, db=v2_db, control=AllowedControl())) as client:
            credentials = {'email': uuid4().hex + '@synthetic.invalid', 'password': 'SynthetiC-password!234'}
            assert client.post('/api/v2/auth/register', json=credentials).status_code == 201
            logged = client.post('/api/v2/auth/login', json=credentials).json()
            user = logged['user_id']
            auth = {'Authorization': 'Bearer ' + logged['token']}
            selected = client.post('/api/v2/resumes', headers=auth,
                                   json={'name': 'Synthetic Rabbit D1', 'profile': {}}).json()
            created = client.post('/api/v2/runs', headers=auth, json={
                'resume_revision_id': selected['revision_id'],
                'application_url': 'https://recruit.synthetic.invalid/form',
                'idempotency_key': 'rabbit-d1-request'})
            assert created.status_code == 202, created.text
            run = created.json()
            assert run['workflow_version'] == WORKFLOW_VERSION
            event = event_for(v2_db, user, run['id'], 1)
            target['event_id'] = event['id']
            with start_worker(celery, perform_ping_check=False, pool='solo', concurrency=1,
                              queues=[name], loglevel='ERROR', WorkController=BoundedWorkerController):
                confirmed_publish(celery, event)
                # Send duplicate delivery too: the Inbox must deduplicate it.
                confirmed_publish(celery, event)
                deadline, state = time.monotonic() + 20, None
                while time.monotonic() < deadline:
                    with v2_db.transaction(user) as conn:
                        state = conn.execute(text('SELECT state FROM zhida.background_jobs WHERE id=:id AND user_id=:u'),
                                             {'id': event['job_id'], 'u': user}).scalar_one()
                    if state == 'awaiting_browser' and len(deliveries) == 2:
                        break
                    time.sleep(.1)
                assert state == 'awaiting_browser'
                assert [item['status'] for item in deliveries] == ['awaiting_browser', 'completed']
                response = client.get(f"/api/v2/runs/{run['id']}", headers=auth)
                assert response.status_code == 200 and response.json()['run_revision'] == 2
                saved = checkpoints(v2_db, user, run['id'])
                assert len(saved) == 1 and saved[0]['event_id'] == event['id']
                assert saved[0]['state']['reason'] == 'browser_not_bound'
                assert saved[0]['lease_epoch'] == 1
                with v2_db.transaction(user) as conn:
                    assert conn.execute(text("""SELECT count(*) FROM zhida.inbox_events
                        WHERE user_id=:u AND event_id=:e AND status='completed'"""),
                        {'u': user, 'e': event['id']}).scalar_one() == 1
                assert not obsolete_calls
    finally:
        task_postrun.disconnect(finished, sender=workflow_task)
        # Explicit unique resources only; never purge a shared application queue.
        with celery.connection_for_write() as connection:
            isolated_queue(connection).delete(if_unused=False, if_empty=False)
            isolated_exchange(connection).delete(if_unused=False)
        celery.close()
