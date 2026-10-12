"""Four Celery queues, transactional dispatch and explicit service entrypoints.

Run with ``celery -A app.backend_v2.workers:celery_app worker -Q workflow.advance``.
Outbox/recovery services: ``python -m app.backend_v2.workers publisher|scheduler``.
Importing the module neither connects to services nor operates a browser/model.
"""
from __future__ import annotations

import argparse
import atexit
import os
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

from sqlalchemy import text

from .messaging import (KINDS, Claim, JobEnvelope, LeaseLost, LeaseStore, OutboxPublisher,
                        QueueStore)


@dataclass(frozen=True)
class WorkResult:
    state: str = 'completed'
    result: Any = None
    effects: Optional[Callable] = None


@dataclass(frozen=True)
class ExecutionContext:
    """Ephemeral fencing context. Never persisted in a graph checkpoint."""
    lease_epoch: int
    event_id: str
    job_id: str
    lease_lost: Callable[[], bool]


class WorkflowEngine(Protocol):
    def advance(self, *, user_id: str, run_id: str, expected_revision: int) -> WorkResult:
        ...


class RetryableWork(RuntimeError):
    def __init__(self, reason: str = 'transient', delay_seconds: Optional[float] = None):
        self.reason, self.delay_seconds = reason, delay_seconds
        super().__init__(reason)


class DeferredWork(RetryableWork):
    """Quota/resource wait; releases the worker without consuming error retries."""


class StaleWork(RuntimeError):
    pass


class UnconfiguredEngine:
    def __init__(self, db=None):
        self.db = db

    def advance(self, *, user_id: str, run_id: str, expected_revision: int) -> WorkResult:
        if self.db is not None:
            with self.db.transaction(user_id) as conn:
                pending = conn.execute(text("""SELECT 1 FROM zhida.confirmation_requests
                    WHERE user_id=:u AND run_id=:r AND status='pending' LIMIT 1"""),
                    {'u': user_id, 'r': run_id}).first()
            if pending:
                return WorkResult('awaiting_user', {'reason': 'pending_confirmation',
                                  'browser_actions_executed': 0, 'langgraph_enabled': False})
        return WorkResult('awaiting_browser', {'reason': 'workflow_engine_not_configured',
                                              'browser_actions_executed': 0, 'langgraph_enabled': False})


class _Heartbeat:
    def __init__(self, queue: QueueStore, claim: Claim, leases: LeaseStore, lease):
        self.queue, self.claim, self.leases, self.lease = queue, claim, leases, lease
        self.stop_event, self.lost = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop_event.wait(15):
            try:
                if not self.queue.renew(self.claim) or (self.lease and not self.leases.renew(self.lease)):
                    self.lost.set()
                    return
            except Exception:
                self.lost.set()
                return

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=2)


class WorkerDispatcher:
    def __init__(self, db, *, handlers: Optional[dict] = None, engine: Optional[WorkflowEngine] = None):
        self.db, self.queue, self.leases = db, QueueStore(db), LeaseStore(db)
        self.handlers, self.engine = handlers or {}, engine or UnconfiguredEngine(db)

    def _advance_workflow(self, job, run, *, reconciliation_required, context):
        engine = self.engine
        if hasattr(engine, 'for_version'):
            engine = engine.for_version(run['workflow_version'])
        version = getattr(engine, 'workflow_version', None)
        if version is not None and run['workflow_version'] != version:
            # Enabling D1 never silently converts an already-created legacy run.
            engine = UnconfiguredEngine(self.db)
        if reconciliation_required and not getattr(engine, 'handles_reconciliation', False):
            return WorkResult('awaiting_browser', {'reason': 'pending_action_reconciliation',
                                                  'browser_actions_executed': 0})
        arguments = dict(user_id=job.user_id, run_id=job.aggregate_id,
                         expected_revision=job.expected_revision)
        if getattr(engine, 'accepts_execution_context', False):
            arguments['execution_context'] = context
        return engine.advance(**arguments)

    def dispatch(self, *, event_id: str, user_id: str) -> dict:
        owner = str(uuid.uuid4())
        claim = self.queue.claim(event_id, user_id=user_id, consumer='zhida-v2-worker', owner_id=owner)
        if claim.status != 'claimed':
            return {'status': claim.status}
        job, resource_lease, heartbeat = claim.job, None, None
        reconciliation_required = False
        try:
            if job.kind == 'workflow.advance':
                resource_lease = self.leases.acquire(resource_id=job.aggregate_id, user_id=job.user_id,
                                                     owner_id=owner)
                if resource_lease is None:
                    raise DeferredWork('lease_busy', 5)
                with self.db.transaction(user_id) as conn:
                    run = conn.execute(text("""SELECT run_revision,status,workflow_version FROM zhida.application_runs
                        WHERE id=:id AND user_id=:user"""),
                        {'id': job.aggregate_id, 'user': user_id}).mappings().first()
                if (run is None or run['run_revision'] != job.expected_revision or
                        run['status'] in {'cancelled', 'ready_for_review', 'failed'}):
                    raise StaleWork('Run revision no longer matches')
                with self.db.transaction(user_id) as conn:
                    changed = conn.execute(text("""UPDATE zhida.application_runs SET status='running',updated_at=now()
                        WHERE id=:id AND user_id=:user AND run_revision=:revision
                          AND status NOT IN ('cancelled','ready_for_review','failed')"""),
                        {'id': job.aggregate_id, 'user': user_id, 'revision': job.expected_revision}).rowcount
                    if changed != 1:
                        raise StaleWork('Run changed before processing')
                    reconciliation_required = bool(conn.execute(text("""SELECT 1 FROM zhida.action_ledger
                        WHERE run_id=:id AND user_id=:user AND status IN ('dispatched','unknown') LIMIT 1"""),
                        {'id': job.aggregate_id, 'user': user_id}).first())
            heartbeat = _Heartbeat(self.queue, claim, self.leases, resource_lease)
            heartbeat.start()
            if job.kind == 'workflow.advance':
                outcome = self._advance_workflow(job, run, reconciliation_required=reconciliation_required,
                    context=ExecutionContext(resource_lease.epoch, job.event_id, job.job_id,
                                             heartbeat.lost.is_set))
            elif job.kind in self.handlers:
                outcome = self.handlers[job.kind](job)
            elif job.kind == 'maintenance':
                outcome = WorkResult(result={'recovered_jobs': self.queue.recover_expired()})
            else:
                outcome = WorkResult('failed', {'reason': 'handler_unavailable', 'kind': job.kind})
            if not isinstance(outcome, WorkResult):
                raise TypeError('Worker handlers must return WorkResult')
            if heartbeat.lost.is_set():
                raise LeaseLost('Worker lost its execution lease')

            def effects(conn):
                if resource_lease:
                    live = conn.execute(text("""SELECT epoch FROM zhida.run_leases
                        WHERE resource_id=:id AND user_id=:user AND owner_id=:owner
                          AND epoch=:epoch AND expires_at>now() FOR UPDATE"""),
                        {'id': job.aggregate_id, 'user': user_id, 'owner': owner,
                         'epoch': resource_lease.epoch}).first()
                    if not live:
                        raise LeaseLost('Workflow lease changed before commit')
                    current = conn.execute(text("""SELECT run_revision,status FROM zhida.application_runs
                        WHERE id=:id AND user_id=:user FOR UPDATE"""),
                        {'id': job.aggregate_id, 'user': user_id}).mappings().first()
                    if (not current or current['run_revision'] != job.expected_revision or
                            current['status'] in {'cancelled', 'ready_for_review', 'failed'}):
                        raise StaleWork('Run changed during processing')
                if outcome.effects:
                    outcome.effects(conn)
                elif job.kind == 'workflow.advance':
                    # The default engine performs no actions and reports an honest waiting state.
                    status = outcome.state if outcome.state in {'awaiting_browser', 'awaiting_user',
                             'reconciling', 'ready_for_review', 'failed', 'cancelled'} else 'running'
                    conn.execute(text("""UPDATE zhida.application_runs SET status=:status,
                        run_revision=run_revision+1,updated_at=now() WHERE id=:id AND user_id=:user
                    """), {'status': status, 'id': job.aggregate_id, 'user': user_id})
                    from .repository import emit_event
                    emit_event(conn, user_id, job.aggregate_id, 'run.waiting', outcome.result or {})

            self.queue.complete(claim, state=outcome.state, result=outcome.result, effects=effects)
            return {'status': outcome.state, 'job_id': job.job_id}
        except StaleWork:
            try:
                self.queue.complete(claim, state='obsolete', result={'reason': 'revision_changed'})
                return {'status': 'obsolete'}
            except LeaseLost:
                return {'status': 'lease_lost'}
        except DeferredWork as exc:
            return self._retry(claim, exc.reason, exc.delay_seconds, count_attempt=False)
        except RetryableWork as exc:
            return self._retry(claim, exc.reason, exc.delay_seconds)
        except LeaseLost:
            # Recovery scheduler wakes the expired job. Do not overwrite its successor.
            return {'status': 'lease_lost'}
        except Exception:
            return self._retry(claim, 'internal')
        finally:
            if heartbeat:
                heartbeat.stop()
            if resource_lease:
                try:
                    self.leases.release(resource_lease)
                except Exception:
                    pass

    def _retry(self, claim: Claim, reason: str, delay: Optional[float] = None,
               *, count_attempt: bool = True) -> dict:
        job = claim.job

        def effects(conn, state):
            if job.kind == 'workflow.advance' and reason != 'lease_busy':
                conn.execute(text("""UPDATE zhida.application_runs SET status=:status,updated_at=now()
                    WHERE id=:id AND user_id=:user AND run_revision=:revision
                      AND status NOT IN ('cancelled','ready_for_review','failed')"""),
                    {'status': state, 'id': job.aggregate_id, 'user': job.user_id,
                     'revision': job.expected_revision})
        try:
            return {'status': self.queue.retry(claim, reason=reason, delay_seconds=delay,
                                              count_attempt=count_attempt, effects=effects)}
        except LeaseLost:
            return {'status': 'lease_lost'}


def _database(url: Optional[str] = None):
    from .db import Database
    configured = url or os.environ.get('ZHIDA_V2_DATABASE_URL')
    if not configured:
        raise RuntimeError('ZHIDA_V2_DATABASE_URL must be configured')
    return Database(configured)


def _configured_handlers(db) -> dict:
    # This import is intentionally lazy; dispatch integrations may import WorkResult.
    try:
        from .dispatch import get_handlers
    except ImportError as exc:
        if exc.name != __package__ + '.dispatch':
            raise
        return {}
    return get_handlers(db)


def _configured_engine(db):
    if os.environ.get('ZHIDA_V2_WORKFLOW_ENABLED', 'false').lower() != 'true':
        return UnconfiguredEngine(db)
    # Lazy import: legacy startup does not import LangGraph or create checkpoints.
    from .workflow import DurableWorkflowEngine
    if os.environ.get('ZHIDA_V2_RECOGNITION_ENABLED', 'false').lower() == 'true':
        # Keep pinned D1 tasks on D1 even after enabling D2 for new tasks.
        from .readonly_workflow import ReadOnlyWorkflowEngine

        class VersionedEngines:
            def for_version(self, version):
                if version=='langgraph-d3-v1' and os.environ.get('ZHIDA_V2_ASSISTED_ENABLED','false').lower()=='true':
                    from .assisted_workflow import AssistedWorkflowEngine
                    return AssistedWorkflowEngine(db)
                if version == 'langgraph-d2-v1':
                    return ReadOnlyWorkflowEngine(db)
                if version == 'langgraph-d1-v1':
                    return DurableWorkflowEngine(db)
                return UnconfiguredEngine(db)

        return VersionedEngines()
    return DurableWorkflowEngine(db)


def create_celery_app(*, database_url: Optional[str] = None, broker_url: Optional[str] = None,
                      handlers: Optional[dict] = None, engine_factory: Optional[Callable] = None):
    from celery import Celery
    from kombu import Exchange, Queue
    broker = broker_url or os.environ.get('ZHIDA_V2_BROKER_URL')
    app = Celery('zhida_v2', broker=broker or 'memory://')
    exchange = Exchange('zhida.v2', type='direct', durable=True)
    app.conf.update(
        task_queues=[Queue(kind, exchange=exchange, routing_key=kind, durable=True) for kind in sorted(KINDS)],
        task_routes={'zhida.v2.'+kind: {'queue': kind, 'routing_key': kind} for kind in KINDS},
        task_serializer='json', accept_content=['json'], result_serializer='json',
        task_ignore_result=True, task_acks_late=True, task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1, broker_transport_options={'confirm_publish': True},
        broker_connection_retry_on_startup=True, task_publish_retry=False,
        task_default_delivery_mode=2, worker_enable_remote_control=False,
        worker_send_task_events=False, task_send_sent_event=False,
        broker_connection_max_retries=3,
    )

    runtime = {}

    def execute(event_id: str, user_id: str):
        # First task in each child process initializes its own pool/model handlers.
        if 'dispatcher' not in runtime:
            db = _database(database_url)
            configured = handlers if handlers is not None else _configured_handlers(db)
            runtime['dispatcher'] = WorkerDispatcher(db, handlers=configured,
                engine=(engine_factory or _configured_engine)(db))
            atexit.register(db.close)
        return runtime['dispatcher'].dispatch(event_id=event_id, user_id=user_id)

    for kind in sorted(KINDS):
        # Each factory instance owns its DB/handler closure. Celery's default
        # shared-task finalizers otherwise copy the first app's closure into
        # later app registries (notably tests and embedded workers).
        app.task(name='zhida.v2.'+kind, autoretry_for=(), shared=False, lazy=False)(execute)
    return app


celery_app = create_celery_app()


def confirmed_publish(app, event: dict):
    """Declare the target queue and use publisher confirms; raise on unroutable."""
    kind = event['kind']
    if kind not in KINDS:
        raise ValueError('Invalid queue route')
    with app.producer_or_acquire() as producer:
        queue = app.amqp.queues[kind]
        queue(producer.channel).declare()
        app.send_task('zhida.v2.'+kind, kwargs={'event_id': event['id'], 'user_id': event['user_id']},
                      task_id=event['id'], queue=kind, routing_key=kind, producer=producer,
                      mandatory=True, retry=False, delivery_mode=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Zhida v2 durable queue services')
    parser.add_argument('service', choices=('publisher', 'scheduler'))
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--interval', type=float, default=2)
    args = parser.parse_args(argv)
    if not 0.1 <= args.interval <= 60:
        parser.error('--interval must be between 0.1 and 60 seconds')
    if args.service == 'publisher' and not os.environ.get('ZHIDA_V2_BROKER_URL'):
        parser.error('ZHIDA_V2_BROKER_URL must be configured')
    db = _database()
    publisher = OutboxPublisher(db, lambda event: confirmed_publish(celery_app, event))
    while True:
        if args.service == 'publisher':
            publisher.publish_batch()
        else:
            QueueStore(db).recover_expired()
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
