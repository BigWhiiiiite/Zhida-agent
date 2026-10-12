"""Transactional jobs/outbox/inbox, with bounded recovery and fenced leases.

No connection or broker operation happens at import. Queue messages are wakeups;
PostgreSQL owns the work state. A delivered event is not a completed event.
"""
from __future__ import annotations

import json
import random
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sqlalchemy import text

KINDS = frozenset({'workflow.advance', 'document.parse', 'rag.index', 'maintenance'})
TERMINAL_STATES = frozenset({'completed', 'failed', 'cancelled', 'obsolete',
                            'awaiting_user', 'awaiting_browser', 'reconciling', 'ready_for_review'})


def _id() -> str:
    return str(uuid.uuid4())


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


@dataclass(frozen=True)
class JobRef:
    job_id: str
    event_id: str


@dataclass(frozen=True)
class JobEnvelope:
    job_id: str
    event_id: str
    user_id: str
    kind: str
    aggregate_id: str
    expected_revision: int
    attempt: int
    payload: dict


@dataclass(frozen=True)
class Claim:
    status: str
    job: Optional[JobEnvelope] = None
    consumer: str = ''
    owner_id: str = ''
    epoch: int = 0


class LeaseLost(RuntimeError):
    pass


def enqueue_job(conn, *, user_id: str, kind: str, aggregate_id: str,
                idempotency_key: str, expected_revision: int = 0,
                payload: Optional[dict] = None) -> JobRef:
    """Caller owns the transaction, including the associated business mutation."""
    if kind not in KINDS or not user_id or not idempotency_key:
        raise ValueError('Invalid job kind, user, or idempotency key')
    job_id, event_id = _id(), _id()
    params = dict(id=job_id, event=event_id, user=user_id, kind=kind,
                  aggregate=aggregate_id, revision=expected_revision,
                  key=idempotency_key, payload=_json(payload or {}))
    inserted = conn.execute(text("""
        INSERT INTO zhida.background_jobs
          (id,user_id,kind,aggregate_id,expected_revision,idempotency_key,payload,current_event_id)
        VALUES (:id,:user,:kind,:aggregate,:revision,:key,CAST(:payload AS jsonb),:event)
        ON CONFLICT(user_id,kind,idempotency_key) DO NOTHING RETURNING id
    """), params).first()
    if inserted:
        conn.execute(text("""INSERT INTO zhida.outbox_events(id,user_id,job_id,kind,attempt)
            VALUES (:event,:user,:id,:kind,0)"""), params)
        return JobRef(job_id, event_id)
    existing = conn.execute(text("""SELECT id,current_event_id,aggregate_id,expected_revision,payload
        FROM zhida.background_jobs WHERE user_id=:user AND kind=:kind AND idempotency_key=:key
    """), params).mappings().one()
    if (existing['aggregate_id'] != aggregate_id or
            existing['expected_revision'] != expected_revision or existing['payload'] != (payload or {})):
        raise ValueError('Idempotency key was already used for different work')
    return JobRef(existing['id'], existing['current_event_id'])


def retry_delay(attempt: int, random_value: Callable[[], float] = random.random) -> float:
    """Three bounded retry intervals. attempt is the zero-based failed attempt."""
    base = (5, 30, 120)[min(max(attempt, 0), 2)]
    return base * (0.8 + 0.4 * random_value())


class QueueStore:
    def __init__(self, db):
        self.db = db

    def claim(self, event_id: str, *, user_id: str, consumer: str,
              owner_id: str, lease_seconds: int = 60) -> Claim:
        if not 1 <= lease_seconds <= 3600:
            raise ValueError('Invalid lease length')
        with self.db.transaction(user_id) as conn:
            row = conn.execute(text("""SELECT j.*,e.attempt AS event_attempt,
                    e.available_at<=now() AS due
                FROM zhida.outbox_events e JOIN zhida.background_jobs j
                  ON j.id=e.job_id AND j.user_id=e.user_id
                WHERE e.id=:event AND e.user_id=:user FOR UPDATE OF j
            """), {'event': event_id, 'user': user_id}).mappings().first()
            if row is None:
                return Claim('missing')
            inbox = conn.execute(text("""SELECT status,lease_expires_at>now() AS active
                FROM zhida.inbox_events WHERE event_id=:event AND consumer=:consumer AND user_id=:user
            """), {'event': event_id, 'consumer': consumer, 'user': user_id}).mappings().first()
            if inbox and inbox['status'] == 'completed':
                return Claim('completed')
            if row['current_event_id'] != event_id or row['state'] in TERMINAL_STATES:
                return Claim('obsolete')
            if not row['due']:
                return Claim('not_due')
            active = conn.execute(text("""SELECT lease_expires_at>now() FROM zhida.background_jobs
                WHERE id=:id AND user_id=:user"""), {'id': row['id'], 'user': user_id}).scalar()
            if active or (inbox and inbox['active']):
                return Claim('busy')
            epoch = row['lease_epoch'] + 1
            params = dict(id=row['id'], event=event_id, user=user_id, consumer=consumer,
                          owner=owner_id, epoch=epoch, seconds=lease_seconds)
            conn.execute(text("""UPDATE zhida.background_jobs SET state='processing',lease_owner=:owner,
                lease_epoch=:epoch,lease_expires_at=now()+:seconds*interval '1 second',updated_at=now()
                WHERE id=:id AND user_id=:user"""), params)
            conn.execute(text("""INSERT INTO zhida.inbox_events
                (consumer,event_id,user_id,status,owner,lease_epoch,lease_expires_at)
                VALUES (:consumer,:event,:user,'processing',:owner,:epoch,now()+:seconds*interval '1 second')
                ON CONFLICT(consumer,event_id) DO UPDATE SET status='processing',owner=EXCLUDED.owner,
                    lease_epoch=EXCLUDED.lease_epoch,lease_expires_at=EXCLUDED.lease_expires_at
                WHERE zhida.inbox_events.user_id=EXCLUDED.user_id"""), params)
            job = JobEnvelope(row['id'], event_id, user_id, row['kind'], row['aggregate_id'],
                              row['expected_revision'], row['attempt'], row['payload'])
            return Claim('claimed', job, consumer, owner_id, epoch)

    @staticmethod
    def _params(claim: Claim) -> dict:
        if claim.status != 'claimed' or claim.job is None:
            raise ValueError('An owned claim is required')
        return dict(id=claim.job.job_id, event=claim.job.event_id, user=claim.job.user_id,
                    consumer=claim.consumer, owner=claim.owner_id, epoch=claim.epoch)

    @staticmethod
    def _lock_owned(conn, params: dict):
        row = conn.execute(text("""SELECT * FROM zhida.background_jobs
            WHERE id=:id AND user_id=:user AND current_event_id=:event
              AND lease_owner=:owner AND lease_epoch=:epoch AND lease_expires_at>now()
              AND state='processing' FOR UPDATE"""), params).mappings().first()
        if row is None:
            raise LeaseLost('The job lease is no longer current')
        return row

    def renew(self, claim: Claim, *, lease_seconds: int = 60) -> bool:
        params = self._params(claim)
        if not 1 <= lease_seconds <= 3600:
            raise ValueError('Invalid lease length')
        params['seconds'] = lease_seconds
        with self.db.transaction(params['user']) as conn:
            result = conn.execute(text("""UPDATE zhida.background_jobs SET
                lease_expires_at=now()+:seconds*interval '1 second'
                WHERE id=:id AND user_id=:user AND current_event_id=:event AND lease_owner=:owner
                  AND lease_epoch=:epoch AND lease_expires_at>now() AND state='processing'
                RETURNING id"""), params).first()
            if result:
                conn.execute(text("""UPDATE zhida.inbox_events SET
                    lease_expires_at=now()+:seconds*interval '1 second'
                    WHERE event_id=:event AND consumer=:consumer AND user_id=:user
                      AND owner=:owner AND lease_epoch=:epoch AND status='processing'"""), params)
            return bool(result)

    def complete(self, claim: Claim, *, state: str = 'completed', result: Any = None,
                 effects: Optional[Callable] = None):
        if state not in TERMINAL_STATES:
            raise ValueError('Completion must be terminal or a durable waiting state')
        params = self._params(claim)
        params.update(state=state, result=_json(result))
        with self.db.transaction(params['user']) as conn:
            self._lock_owned(conn, params)
            if effects:
                effects(conn)
            conn.execute(text("""UPDATE zhida.background_jobs SET state=:state,result=CAST(:result AS jsonb),
                lease_owner=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=:id AND user_id=:user
            """), params)
            self._finish_inbox(conn, params)

    @staticmethod
    def _finish_inbox(conn, params: dict):
        conn.execute(text("""UPDATE zhida.inbox_events SET status='completed',completed_at=now(),
            lease_expires_at=NULL WHERE consumer=:consumer AND event_id=:event AND user_id=:user
            AND owner=:owner AND lease_epoch=:epoch"""), params)

    def retry(self, claim: Claim, *, reason: str, delay_seconds: Optional[float] = None,
              max_retries: int = 3, count_attempt: bool = True,
              effects: Optional[Callable] = None) -> str:
        params = self._params(claim)
        # Reasons are classifications, not raw exception text (which may contain personal data).
        params['reason'] = reason if reason in {'transient', 'rate_limited', 'connection', 'lease_busy',
                                              'handler_unavailable', 'embedding_unavailable', 'internal'} else 'internal'
        with self.db.transaction(params['user']) as conn:
            row = self._lock_owned(conn, params)
            if count_attempt and row['attempt'] >= max_retries:
                if effects:
                    effects(conn, 'failed')
                conn.execute(text("""UPDATE zhida.background_jobs SET state='failed',last_error=:reason,
                    lease_owner=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=:id AND user_id=:user
                """), params)
                self._finish_inbox(conn, params)
                return 'failed'
            delay = retry_delay(row['attempt']) if delay_seconds is None else delay_seconds
            if not 0 <= delay <= 86400:
                raise ValueError('Retry delay is out of bounds')
            if effects:
                effects(conn, 'retry_wait')
            params.update(next_event=_id(), next_attempt=row['attempt'] + int(count_attempt), delay=delay, kind=row['kind'])
            conn.execute(text("""INSERT INTO zhida.outbox_events
                (id,user_id,job_id,kind,attempt,available_at)
                VALUES (:next_event,:user,:id,:kind,:next_attempt,now()+:delay*interval '1 second')
            """), params)
            conn.execute(text("""UPDATE zhida.background_jobs SET state='retry_wait',attempt=:next_attempt,
                current_event_id=:next_event,available_at=now()+:delay*interval '1 second',last_error=:reason,
                lease_owner=NULL,lease_expires_at=NULL,updated_at=now() WHERE id=:id AND user_id=:user
            """), params)
            self._finish_inbox(conn, params)
            return 'retry_wait'

    def recover_expired(self, *, limit: int = 100) -> int:
        """Wake lost jobs; never replay a browser command. Requires service DB role."""
        with self.db.transaction() as conn:
            # Transaction-scoped leader election; another scheduler may scan after
            # this transaction finishes, but never races the current recovery pass.
            if not conn.execute(text('SELECT pg_try_advisory_xact_lock(15000, 2)')).scalar():
                return 0
            rows = conn.execute(text("""SELECT j.id,j.user_id,j.current_event_id,j.state,
                    j.kind,j.aggregate_id,j.expected_revision
                FROM zhida.background_jobs j JOIN zhida.outbox_events e ON e.id=j.current_event_id
                WHERE (j.state='processing' AND j.lease_expires_at<=now())
                   OR (j.state IN ('queued','retry_wait') AND j.available_at<=now()
                       AND e.published_at<now()-interval '60 seconds')
                ORDER BY j.updated_at LIMIT :limit FOR UPDATE OF j SKIP LOCKED
            """), {'limit': min(max(limit, 1), 1000)}).mappings().all()
            for row in rows:
                params = dict(id=row['id'], user=row['user_id'], event=row['current_event_id'])
                # Queue metadata can be scanned by the service role; private run
                # tables still enforce RLS, so each business repair sets its owner.
                conn.execute(text("SELECT set_config('zhida.user_id', :user, true)"), params)
                conn.execute(text("""UPDATE zhida.background_jobs SET state='queued',lease_owner=NULL,
                    lease_expires_at=NULL,updated_at=now() WHERE id=:id AND user_id=:user"""), params)
                conn.execute(text("""UPDATE zhida.inbox_events SET lease_expires_at=now()-interval '1 second'
                    WHERE event_id=:event AND user_id=:user AND status='processing'"""), params)
                conn.execute(text("""UPDATE zhida.outbox_events SET published_at=NULL,
                    publishing_owner=NULL,publish_lease_expires_at=NULL,available_at=now()
                    WHERE id=:event AND user_id=:user"""), params)
                if row['kind'] == 'workflow.advance':
                    params.update(run=row['aggregate_id'], revision=row['expected_revision'])
                    conn.execute(text("""UPDATE zhida.application_runs SET status=CASE WHEN EXISTS(
                        SELECT 1 FROM zhida.action_ledger a WHERE a.run_id=:run AND a.user_id=:user
                          AND a.status IN ('dispatched','unknown')) THEN 'reconciling' ELSE 'queued' END,
                        updated_at=now() WHERE id=:run AND user_id=:user AND run_revision=:revision
                          AND status IN ('running','retry_wait','reconciling')"""), params)
            return len(rows)


class OutboxPublisher:
    """publish callable must return only after a confirmed broker publication."""
    def __init__(self, db, publish_callable: Callable[[dict], None]):
        self.db, self.publish = db, publish_callable

    def publish_batch(self, *, limit: int = 100) -> int:
        owner = _id()
        # Short DB claim; do not hold transactions while calling the broker.
        with self.db.transaction() as conn:
            rows = conn.execute(text("""SELECT id,user_id,job_id,kind,attempt FROM zhida.outbox_events
                WHERE published_at IS NULL AND available_at<=now()
                  AND (publish_lease_expires_at IS NULL OR publish_lease_expires_at<=now())
                ORDER BY available_at LIMIT :limit FOR UPDATE SKIP LOCKED
            """), {'limit': min(max(limit, 1), 1000)}).mappings().all()
            for row in rows:
                conn.execute(text("""UPDATE zhida.outbox_events SET publishing_owner=:owner,
                    publish_lease_expires_at=now()+interval '60 seconds' WHERE id=:id
                """), {'owner': owner, 'id': row['id']})
        published = 0
        for row in rows:
            try:
                self.publish(dict(row))
            except Exception:
                # Leave the lease for bounded retry after a broker outage. Do not log payload/errors.
                continue
            with self.db.transaction() as conn:
                changed = conn.execute(text("""UPDATE zhida.outbox_events SET published_at=now(),
                    publishing_owner=NULL,publish_lease_expires_at=NULL
                    WHERE id=:id AND publishing_owner=:owner AND published_at IS NULL
                """), {'id': row['id'], 'owner': owner}).rowcount
                published += changed
        return published


@dataclass(frozen=True)
class ResourceLease:
    resource_id: str
    user_id: str
    owner_id: str
    epoch: int
    browser: bool = False


class LeaseStore:
    """DB leases fence stale state commits; a lease cannot cancel website effects."""
    def __init__(self, db):
        self.db = db

    def acquire(self, *, resource_id: str, user_id: str, owner_id: str,
                seconds: int = 60, browser: bool = False) -> Optional[ResourceLease]:
        if not 1 <= seconds <= 3600:
            raise ValueError('Invalid lease length')
        table = 'runner_leases' if browser else 'run_leases'
        extra = 'AND NOT zhida.runner_leases.needs_reconciliation' if browser else ''
        with self.db.transaction(user_id) as conn:
            row = conn.execute(text(f"""INSERT INTO zhida.{table}
                (resource_id,user_id,owner_id,epoch,expires_at)
                VALUES (:resource,:user,:owner,1,now()+:seconds*interval '1 second')
                ON CONFLICT(resource_id) DO UPDATE SET owner_id=EXCLUDED.owner_id,
                    epoch=zhida.{table}.epoch+1,expires_at=EXCLUDED.expires_at
                WHERE zhida.{table}.user_id=EXCLUDED.user_id AND zhida.{table}.expires_at<=now() {extra}
                RETURNING epoch"""), dict(resource=resource_id, user=user_id, owner=owner_id,
                                           seconds=seconds)).first()
            return ResourceLease(resource_id, user_id, owner_id, row[0], browser) if row else None

    def renew(self, lease: ResourceLease, *, seconds: int = 60) -> bool:
        if not 1 <= seconds <= 3600:
            raise ValueError('Invalid lease length')
        table = 'runner_leases' if lease.browser else 'run_leases'
        with self.db.transaction(lease.user_id) as conn:
            result = conn.execute(text(f"""UPDATE zhida.{table}
                SET expires_at=now()+:seconds*interval '1 second'
                WHERE resource_id=:resource AND user_id=:user AND owner_id=:owner
                  AND epoch=:epoch AND expires_at>now()"""),
                dict(resource=lease.resource_id, user=lease.user_id, owner=lease.owner_id,
                     epoch=lease.epoch, seconds=seconds))
            return result.rowcount == 1

    def release(self, lease: ResourceLease, *, unknown: bool = False) -> bool:
        table = 'runner_leases' if lease.browser else 'run_leases'
        extra = ',needs_reconciliation=true' if lease.browser and unknown else ''
        with self.db.transaction(lease.user_id) as conn:
            result = conn.execute(text(f"""UPDATE zhida.{table} SET expires_at=now() {extra}
                WHERE resource_id=:resource AND user_id=:user AND owner_id=:owner AND epoch=:epoch
            """), dict(resource=lease.resource_id, user=lease.user_id, owner=lease.owner_id,
                       epoch=lease.epoch))
            return result.rowcount == 1
