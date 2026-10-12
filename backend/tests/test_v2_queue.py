import pytest

from app.backend_v2.messaging import retry_delay
from app.backend_v2.workers import UnconfiguredEngine, WorkResult, create_celery_app


def test_retry_is_bounded_and_jittered():
    assert retry_delay(0, lambda: 0) == 4
    assert retry_delay(1, lambda: 0.5) == 30
    assert retry_delay(500, lambda: 1) == pytest.approx(144)


def test_unconfigured_engine_does_not_claim_browser_success():
    result = UnconfiguredEngine().advance(user_id='test', run_id='run', expected_revision=1)
    assert isinstance(result, WorkResult)
    assert result.state == 'awaiting_browser'
    assert result.result['browser_actions_executed'] == 0


def test_four_queues_use_json_late_ack_and_no_automatic_business_retry():
    app = create_celery_app(broker_url='memory://')
    expected = {'workflow.advance', 'document.parse', 'rag.index', 'maintenance'}
    assert set(app.amqp.queues) == expected
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_acks_late
    assert app.conf.broker_transport_options['confirm_publish']
    assert app.conf.accept_content == ['json']
    assert all('zhida.v2.' + kind in app.tasks for kind in expected)


def test_factory_task_registration_keeps_database_and_handlers_per_app(monkeypatch):
    from app.backend_v2 import workers

    class FakeDatabase:
        def __init__(self, url):
            self.url = url

        def close(self):
            pass

    class FakeDispatcher:
        def __init__(self, db, handlers, engine):
            self.db, self.handlers = db, handlers

        def dispatch(self, **_):
            return self.db.url, self.handlers['identity']

    monkeypatch.setattr(workers, '_database', FakeDatabase)
    monkeypatch.setattr(workers, 'WorkerDispatcher', FakeDispatcher)
    first = create_celery_app(database_url='first-db', broker_url='memory://', handlers={'identity': 'first'})
    second = create_celery_app(database_url='second-db', broker_url='memory://', handlers={'identity': 'second'})
    try:
        assert first.tasks['zhida.v2.document.parse'].run('event', 'user') == ('first-db', 'first')
        assert second.tasks['zhida.v2.document.parse'].run('event', 'user') == ('second-db', 'second')
    finally:
        first.close()
        second.close()
