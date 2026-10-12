"""Explicit read-only Runner evidence ingestion; never opens or probes a browser.

Only an already bound session at the expected connection/run revision may
publish. The API accepts a sanitized BrowserSnapshot, not arbitrary selectors
as commands, model instructions, cookies or executable answer mappings.
"""
from datetime import datetime, timezone
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import text

from .messaging import enqueue_job
from .repository import Conflict, digest, emit_event, encode, require_row, validate_application_url

D2_VERSION = 'langgraph-d2-v1'


def document_url(url):
    parts = urlsplit(validate_application_url(url))
    return (parts.scheme.lower(), parts.hostname.lower(), parts.port or 443,
            parts.path or '/', parts.query, parts.fragment)


class ObservationStore:
    def __init__(self, db):
        self.db = db

    def record(self, user_id, run_id, *, expected_revision, browser_session_id,
               connection_epoch, document_id, capture_id, captured_at, snapshot):
        from .observation_adapter import adapt_browser_snapshot
        if not all(isinstance(x, str) and 0 < len(x) <= 200 for x in
                   (browser_session_id, document_id, capture_id)):
            raise ValueError('观察必须包含明确的浏览器、文档和采集标识')
        if not isinstance(connection_epoch, int) or isinstance(connection_epoch, bool) or connection_epoch < 1:
            raise ValueError('浏览器连接版本无效')
        if not isinstance(snapshot, dict) or len(encode(snapshot)) > 750_000:
            raise ValueError('观察快照无效或过大')
        try:
            captured = datetime.fromisoformat(captured_at.replace('Z', '+00:00'))
            if captured.tzinfo is None:
                raise ValueError()
        except (ValueError, AttributeError, TypeError):
            raise ValueError('采集时间必须包含时区') from None
        # URL is deliberately stricter than same-domain matching: never attach a
        # homepage, a different application or another applicant's open document.
        url = validate_application_url(snapshot.get('url', ''))
        sanitized = adapt_browser_snapshot(snapshot)
        identity = str(uuid5(NAMESPACE_URL, encode([user_id, run_id, 'capture', capture_id])))
        envelope = {'schema_version': 'runner-observation-v1', 'capture_id': capture_id,
                    'input_run_revision': expected_revision, 'browser_session_id': browser_session_id,
                    'connection_epoch': connection_epoch, 'document_id': document_id,
                    'captured_at': captured.isoformat(), 'url': url, 'snapshot': sanitized}
        request_hash = digest(envelope)
        with self.db.transaction(user_id) as cx:
            require_row(cx, 'SELECT id FROM zhida.users WHERE id=:u FOR UPDATE', {'u': user_id})
            run = require_row(cx, 'SELECT * FROM zhida.application_runs WHERE user_id=:u AND id=:r FOR UPDATE',
                              {'u': user_id, 'r': run_id})
            old = cx.execute(text('SELECT evidence FROM zhida.form_observations WHERE user_id=:u AND id=:id'),
                             {'u': user_id, 'id': identity}).scalar()
            if old:
                if old.get('request_hash') != request_hash:
                    raise Conflict('同一采集标识不能对应不同页面证据')
                return {'observation_id': identity, 'duplicate': True,
                        'run_revision': run['run_revision'], 'browser_actions_executed': 0}
            if run['workflow_version'] != D2_VERSION:
                raise Conflict('此任务不是只读识别 D2 任务，不会改变原有流程')
            if run['run_revision'] != expected_revision or run['status'] in {'failed','cancelled','ready_for_review'}:
                raise Conflict('任务版本已变化或已结束，请重新读取状态')
            if not -5 <= (datetime.now(timezone.utc) - captured).total_seconds() <= 300:
                raise Conflict('页面证据已过期，请重新采集当前文档')
            if run['browser_session_id'] != browser_session_id or document_url(run['application_url']) != document_url(url):
                raise Conflict('观察页面与本任务绑定的招聘填写页不一致')
            browser = require_row(cx, 'SELECT * FROM zhida.browser_sessions WHERE user_id=:u AND id=:b FOR UPDATE',
                                  {'u': user_id, 'b': browser_session_id})
            if browser['connection_epoch'] != connection_epoch:
                raise Conflict('浏览器连接已变化，请重新采集')
            legacy_id = browser['binding'].get('legacy_session_id')
            if legacy_id and snapshot.get('session_id') != legacy_id:
                raise Conflict('快照不是本任务指定的招聘窗口')
            if cx.execute(text("""SELECT 1 FROM zhida.action_ledger WHERE user_id=:u AND run_id=:r
                    AND status IN ('dispatched','unknown') LIMIT 1"""), {'u': user_id, 'r': run_id}).first():
                raise Conflict('存在结果未明的操作，请先对账；不能用新快照清除该状态')
            number = cx.execute(text('SELECT COALESCE(max(observation_revision),0)+1 FROM zhida.form_observations '
                                     'WHERE user_id=:u AND run_id=:r'), {'u': user_id, 'r': run_id}).scalar_one()
            cx.execute(text("""INSERT INTO zhida.form_observations
                (id,user_id,run_id,document_id,observation_revision,coverage,evidence)
                VALUES(:id,:u,:r,:d,:n,'{}',CAST(:e AS jsonb))"""),
                {'id': identity, 'u': user_id, 'r': run_id, 'd': document_id, 'n': number,
                 'e': encode({**envelope, 'request_hash': request_hash})})
            # A new document invalidates all unanswered old questions and plans.
            # Already confirmed facts survive, and are rematched with new options.
            cx.execute(text("UPDATE zhida.confirmation_requests SET status='superseded' "
                            "WHERE user_id=:u AND run_id=:r AND status='pending'"), {'u': user_id, 'r': run_id})
            cx.execute(text("UPDATE zhida.fill_plans SET status='invalidated' WHERE user_id=:u AND run_id=:r AND status='planned'"),
                       {'u': user_id, 'r': run_id})
            cx.execute(text("UPDATE zhida.action_ledger SET status='invalidated' WHERE user_id=:u AND run_id=:r AND status='planned'"),
                       {'u': user_id, 'r': run_id})
            revision = expected_revision + 1
            cx.execute(text("UPDATE zhida.application_runs SET status='queued',run_revision=:v,updated_at=now() WHERE user_id=:u AND id=:r"),
                       {'v': revision, 'u': user_id, 'r': run_id})
            job = enqueue_job(cx, user_id=user_id, kind='workflow.advance', aggregate_id=run_id,
                              expected_revision=revision, idempotency_key=f'observation:{identity}',
                              payload={'reason': 'observation_recorded', 'observation_id': identity})
            emit_event(cx, user_id, run_id, 'form.observation_recorded',
                       {'observation_id': identity, 'run_revision': revision, 'browser_actions_executed': 0})
            return {'observation_id': identity, 'duplicate': False, 'run_revision': revision,
                    'job_id': job.job_id, 'browser_actions_executed': 0}

    def report(self, user_id, run_id):
        with self.db.transaction(user_id) as cx:
            run = require_row(cx, 'SELECT run_revision,browser_session_id,application_url FROM zhida.application_runs WHERE user_id=:u AND id=:r',
                              {'u': user_id, 'r': run_id})
            row = cx.execute(text('SELECT id,observation_id,run_revision,report FROM zhida.form_analysis_reports '
                                  'WHERE user_id=:u AND run_id=:r ORDER BY run_revision DESC LIMIT 1'),
                             {'u': user_id, 'r': run_id}).mappings().first()
            reasons = []
            if row:
                if row['run_revision'] != run['run_revision']:
                    reasons.append('run_revision_changed')
                observed = cx.execute(text('SELECT evidence FROM zhida.form_observations WHERE user_id=:u AND id=:o'),
                                      {'u': user_id, 'o': row['observation_id']}).scalar_one()
                epoch = cx.execute(text('SELECT connection_epoch FROM zhida.browser_sessions WHERE user_id=:u AND id=:b'),
                                   {'u': user_id, 'b': run['browser_session_id']}).scalar()
                if epoch != observed.get('connection_epoch') or run['browser_session_id'] != observed.get('browser_session_id'):
                    reasons.append('browser_binding_changed')
                try:
                    captured = datetime.fromisoformat(observed['captured_at'])
                    if captured.tzinfo is None or not -5 <= (datetime.now(timezone.utc)-captured).total_seconds() <= 300:
                        reasons.append('observation_expired')
                    if document_url(observed['url']) != document_url(run['application_url']):
                        reasons.append('document_changed')
                except (KeyError, ValueError, TypeError):
                    reasons.append('observation_invalid')
                refs = {ref for s in row['report']['plan']['suggestions'] for ref in s['fact_revision_refs']}
                if refs:
                    current = set(cx.execute(text("""SELECT fr.id FROM zhida.fact_revisions fr
                        JOIN zhida.profile_facts f ON f.current_revision_id=fr.id AND f.user_id=fr.user_id
                        WHERE f.user_id=:u AND fr.id=ANY(CAST(:refs AS text[]))
                        AND fr.confirmation='confirmed' AND (fr.valid_until IS NULL OR fr.valid_until>now())"""),
                        {'u': user_id, 'refs': list(refs)}).scalars())
                    if current != refs:
                        reasons.append('fact_revision_changed_or_expired')
            return {'analysis': dict(row) if row else None,
                    'stale': bool(reasons), 'stale_reasons': reasons,
                    'run_revision': run['run_revision'], 'browser_actions_executed': 0}
