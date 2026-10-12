"""Owned Outbox event -> isolated real RabbitMQ queue -> Celery -> PostgreSQL.

Never drain a shared test outbox or consume a shared physical application queue:
the test database can retain older synthetic users/jobs from previous suites.
"""
import faulthandler
import os
import sys
import threading
import time
import uuid
from urllib.parse import unquote, urlparse

import pytest
from sqlalchemy import text

from app.backend_v2.messaging import enqueue_job
from app.backend_v2.workers import WorkResult, confirmed_publish, create_celery_app


def test_real_broker_executes_durable_job(v2_db):
    broker = os.getenv('ZHIDA_TEST_BROKER_URL')
    if not broker:
        pytest.skip('Set ZHIDA_TEST_BROKER_URL for a dedicated RabbitMQ test vhost')
    parsed = urlparse(broker)
    if parsed.scheme not in {'amqp', 'amqps'} or not unquote(parsed.path).lstrip('/').startswith('zhida_test'):
        pytest.fail('RabbitMQ integration requires a vhost beginning with zhida_test')
    from celery.contrib.testing.worker import TestWorkController, start_worker
    from kombu import Exchange, Queue

    user = 'rabbit-test-' + uuid.uuid4().hex
    with v2_db.transaction() as conn:
        conn.execute(text('INSERT INTO zhida.users(id) VALUES (:id)'), {'id': user})
        ref = enqueue_job(conn, user_id=user, kind='document.parse', aggregate_id='synthetic-fixture',
                          idempotency_key='one')
    with v2_db.transaction(user) as conn:
        event = dict(conn.execute(text("""SELECT id,user_id,job_id,kind,attempt
            FROM zhida.outbox_events WHERE id=:event AND user_id=:user AND job_id=:job"""),
            {'event': ref.event_id, 'user': user, 'job': ref.job_id}).mappings().one())
    handled = []

    def parse_fixture(job):
        assert job.job_id == ref.job_id and job.user_id == user
        handled.append(job.job_id)
        return WorkResult(result={'fixture': True})

    app = create_celery_app(database_url=os.environ['ZHIDA_TEST_DATABASE_URL'], broker_url=broker,
                            handlers={'document.parse': parse_fixture})
    name = 'zhida.test.document.' + uuid.uuid4().hex
    isolated_exchange = Exchange(name, type='direct', durable=True, auto_delete=False)
    isolated_queue = Queue(name, exchange=isolated_exchange, routing_key='document.parse',
                           alias='document.parse', durable=True, auto_delete=False)
    app.conf.task_queues = (isolated_queue,)
    app.conf.task_routes = {'zhida.v2.document.parse': {'queue': name, 'routing_key': 'document.parse'}}
    app.amqp.queues = app.amqp.Queues((isolated_queue,))
    assert app.amqp.queues[name] is app.amqp.queues['document.parse']

    class BoundedWorkerController(TestWorkController):
        def ensure_started(self):
            if self._on_started.wait(20):
                return
            faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
            shutdown = threading.Thread(target=self.terminate, daemon=True)
            shutdown.start()
            shutdown.join(timeout=3)
            raise TimeoutError('Synthetic document Celery worker did not start within 20 seconds')

    try:
        with start_worker(app, perform_ping_check=False, pool='solo', concurrency=1,
                          queues=[name], loglevel='ERROR', WorkController=BoundedWorkerController):
            # Target this committed event, not a global ORDER BY available_at
            # batch which may contain 100 older fixtures and omit our new job.
            confirmed_publish(app, event)
            deadline = time.monotonic() + 15
            state = None
            while time.monotonic() < deadline:
                with v2_db.transaction(user) as conn:
                    state = conn.execute(text('SELECT state FROM zhida.background_jobs WHERE id=:id AND user_id=:user'),
                                         {'id': ref.job_id, 'user': user}).scalar()
                if state == 'completed':
                    break
                time.sleep(0.1)
            assert state == 'completed'
            assert handled == [ref.job_id]
            with v2_db.transaction(user) as conn:
                assert conn.execute(text("SELECT count(*) FROM zhida.inbox_events WHERE event_id=:id AND user_id=:user AND status='completed'"),
                                    {'id': ref.event_id, 'user': user}).scalar() == 1
    finally:
        try:
            # Only this UUID's resources. Never purge or delete shared queues.
            with app.connection_for_write(connect_timeout=5) as connection:
                isolated_queue(connection).delete(if_unused=False, if_empty=False)
                isolated_exchange(connection).delete(if_unused=False)
        finally:
            app.close()
            with v2_db.transaction(user) as conn:
                for table in ('inbox_events', 'outbox_events', 'background_jobs'):
                    conn.execute(text('DELETE FROM zhida.' + table + ' WHERE user_id=:user'), {'user': user})
                conn.execute(text('DELETE FROM zhida.users WHERE id=:user'), {'user': user})
