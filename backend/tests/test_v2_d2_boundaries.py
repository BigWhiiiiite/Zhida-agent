"""D2 evidence freshness/provenance boundaries; synthetic PostgreSQL only."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.backend_v2 import observation_store
from app.backend_v2.observation_store import ObservationStore
from app.backend_v2.readonly_workflow import ReadOnlyWorkflowEngine
from app.backend_v2.repository import NotFound, Repository
from app.backend_v2.workers import StaleWork
from test_v2_readonly_workflow import (
    SyntheticRetriever, advance, capture, d2_run,
)


def test_capture_expiring_during_analysis_cannot_commit_report_or_questions(v2_db, d2_run):
    repo, owner, run, _ = d2_run
    _, inputs = capture(v2_db, d2_run)
    clock = [datetime.fromisoformat(inputs['captured_at']) + timedelta(seconds=299)]
    result = ReadOnlyWorkflowEngine(v2_db, clock=lambda: clock[0], retriever=SyntheticRetriever()).advance(
        user_id=owner, run_id=run['id'], expected_revision=repo.get_run(owner, run['id'])['run_revision'])
    assert result.result['analysis_available']
    clock[0] += timedelta(seconds=2)
    with pytest.raises(StaleWork), v2_db.transaction(owner) as cx:
        result.effects(cx)
    assert ObservationStore(v2_db).report(owner, run['id'])['analysis'] is None
    assert not repo.get_run(owner, run['id'])['questions']
    assert repo.get_run(owner, run['id'])['run_revision'] == 2
    with v2_db.transaction(owner) as cx:
        assert cx.execute(text('SELECT count(*) FROM zhida.workflow_checkpoints WHERE user_id=:u AND run_id=:r'),
                          {'u': owner, 'r': run['id']}).scalar_one() == 0


@pytest.mark.parametrize('change,reason', [
    ('epoch', 'browser_binding_changed'),
    ('url', 'document_changed'),
    ('expiry', 'observation_expired'),
])
def test_report_freshness_checks_binding_and_age_even_without_run_revision_change(
        v2_db, d2_run, monkeypatch, change, reason):
    repo, owner, run, _ = d2_run
    _, inputs = capture(v2_db, d2_run)
    advance(v2_db, d2_run)
    store = ObservationStore(v2_db)
    before = store.report(owner, run['id'])
    assert before['analysis'] is not None and before['stale'] is False
    if change == 'epoch':
        with v2_db.transaction(owner) as cx:
            cx.execute(text('UPDATE zhida.browser_sessions SET connection_epoch=connection_epoch+1 WHERE user_id=:u AND id=:b'),
                       {'u': owner, 'b': run['browser_session_id']})
    elif change == 'url':
        with v2_db.transaction(owner) as cx:
            cx.execute(text("UPDATE zhida.application_runs SET application_url=application_url||'&different=1' WHERE user_id=:u AND id=:r"),
                       {'u': owner, 'r': run['id']})
    else:
        later = datetime.fromisoformat(inputs['captured_at']) + timedelta(minutes=6)

        class LaterDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return later.astimezone(tz or timezone.utc)

        monkeypatch.setattr(observation_store, 'datetime', LaterDatetime)
    after = store.report(owner, run['id'])
    assert after['run_revision'] == before['run_revision']
    assert after['stale'] is True and reason in after['stale_reasons']
    assert after['browser_actions_executed'] == 0


def test_report_rejects_cross_tenant_reads_without_disclosing_analysis(v2_db, d2_run):
    _, owner, run, _ = d2_run
    capture(v2_db, d2_run)
    advance(v2_db, d2_run)
    other = 'd2-boundary-' + uuid4().hex
    Repository(v2_db).ensure_user(other)
    assert ObservationStore(v2_db).report(owner, run['id'])['analysis'] is not None
    with pytest.raises(NotFound):
        ObservationStore(v2_db).report(other, run['id'])


def test_capture_rejects_cross_tenant_write_without_changing_original_report(v2_db, d2_run):
    _, owner, run, _ = d2_run
    _, inputs = capture(v2_db, d2_run)
    advance(v2_db, d2_run)
    store = ObservationStore(v2_db)
    before = store.report(owner, run['id'])
    other = 'd2-boundary-' + uuid4().hex
    Repository(v2_db).ensure_user(other)
    with pytest.raises(NotFound):
        store.record(other, run['id'], **{**inputs, 'capture_id': uuid4().hex})
    assert store.report(owner, run['id']) == before


def test_report_marks_expired_fact_stale_without_implicitly_rewriting_it(v2_db, d2_run):
    repo, owner, run, _ = d2_run
    fact = repo.save_fact(owner, semantic_key='name', subject_id='person', scope_key='global',
                          value='SYNTHETIC-NAME', valid_until=datetime.now(timezone.utc) + timedelta(hours=1))
    capture(v2_db, d2_run)
    advance(v2_db, d2_run)
    store = ObservationStore(v2_db)
    before = store.report(owner, run['id'])
    assert before['analysis']['report']['plan']['suggestions'] and not before['stale']
    with v2_db.transaction(owner) as cx:
        cx.execute(text("UPDATE zhida.fact_revisions SET valid_until=now()-interval '1 minute' WHERE user_id=:u AND fact_id=:id"),
                   {'u': owner, 'id': fact['id']})
    after = store.report(owner, run['id'])
    assert after['run_revision'] == before['run_revision']
    assert 'fact_revision_changed_or_expired' in after['stale_reasons']
    assert after['analysis'] == before['analysis']
