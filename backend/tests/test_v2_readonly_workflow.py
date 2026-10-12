"""D2 end-to-end synthetic evidence → real graph → durable report/confirmation.

No ATS, external LLM or real user's profile is touched. The native API tests
publish snapshots; they do not claim the compatibility workbench is switched.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.backend_v2.api import create_app
from app.backend_v2.observation_store import D2_VERSION, ObservationStore
from app.backend_v2.readonly_workflow import ReadOnlyWorkflowEngine
from app.backend_v2.repository import Conflict, NotFound, Repository, encode
from app.backend_v2.settings import Settings
from app.backend_v2.workers import StaleWork, WorkerDispatcher, _configured_engine
from test_v2_api import AllowedControl


URL = 'https://jobs.synthetic.invalid/resumeEdit?postId=123'


def raw_field(label='姓名', selector='#name', **changes):
    return {'selector': selector, 'label': label, 'question_text': label, 'field_type': 'text',
            'control_kind': 'text', 'label_source': 'explicit', 'required': True,
            'section_path': ['个人基本信息'],
            'question_candidates': [{'text': label, 'source': 'explicit', 'owned': True}],
            'observation': {'question_status': 'verified', 'options_status': 'not_applicable',
                            'record_status': 'not_applicable'}, **changes}


def snapshot(*fields):
    return {'session_id': 'synthetic-legacy-session', 'url': URL, 'title': '合成简历填写页',
            'fields': list(fields or [raw_field()]), 'context_fields': [],
            'extraction_report': {'capture_status': 'observed', 'observed_controls': len(fields) or 1,
                'captured_controls': len(fields) or 1, 'unmapped_controls': 0,
                'pending_sections': [], 'embedded_regions': 0, 'unread_shadow_regions': 0}}


class SyntheticRetriever:
    def __init__(self):
        self.calls = []

    def search(self, user_id, query, **kwargs):
        self.calls.append((user_id, query, kwargs))
        return {'mode': 'not_indexed', 'evidence': []}


@pytest.fixture
def d2_run(v2_db):
    owner, browser, device = ['d2-' + uuid4().hex for _ in range(3)]
    repo = Repository(v2_db)
    repo.ensure_user(owner)
    selected = repo.save_resume(owner, name='Synthetic D2', profile={'name': 'SYNTHETIC-RESUME-NAME'})
    with v2_db.transaction(owner) as cx:
        cx.execute(text("INSERT INTO zhida.devices(id,user_id,name) VALUES(:id,:u,'Synthetic device')"),
                   {'id': device, 'u': owner})
        cx.execute(text("""INSERT INTO zhida.browser_sessions(id,user_id,device_id,driver,binding)
            VALUES(:id,:u,:d,'synthetic',CAST(:b AS jsonb))"""),
            {'id': browser, 'u': owner, 'd': device, 'b': encode({'legacy_session_id': 'synthetic-legacy-session'})})
    run = repo.create_run(owner, resume_revision_id=selected['revision_id'], application_url=URL,
                         idempotency_key='synthetic-d2', browser_session_id=browser, workflow_version=D2_VERSION)
    return repo, owner, run, selected


def capture(db, fixture, *, raw=None, **changes):
    repo, owner, run, _ = fixture
    inputs = {'expected_revision': repo.get_run(owner, run['id'])['run_revision'],
              'browser_session_id': run['browser_session_id'], 'connection_epoch': 1,
              'document_id': 'synthetic-doc-1', 'capture_id': uuid4().hex,
              'captured_at': datetime.now(timezone.utc).isoformat(), 'snapshot': raw or snapshot(), **changes}
    return ObservationStore(db).record(owner, run['id'], **inputs), inputs


def advance(db, fixture, *, commit=True, retriever=None):
    repo, owner, run, _ = fixture
    result = ReadOnlyWorkflowEngine(db, retriever=retriever or SyntheticRetriever()).advance(
        user_id=owner, run_id=run['id'], expected_revision=repo.get_run(owner, run['id'])['run_revision'])
    if commit:
        with db.transaction(owner) as cx:
            result.effects(cx)
    return result


def test_snapshot_capture_is_idempotent_and_bound_to_window_document_and_revision(v2_db, d2_run):
    repo, owner, run, _ = d2_run
    recorded, inputs = capture(v2_db, d2_run)
    assert recorded['run_revision'] == 2
    store = ObservationStore(v2_db)
    repeated = store.record(owner, run['id'], **inputs)
    assert repeated['observation_id'] == recorded['observation_id'] and repeated['duplicate']
    with pytest.raises(Conflict):
        store.record(owner, run['id'], **{**inputs, 'document_id': 'different'})
    for change in ({'connection_epoch': 2}, {'browser_session_id': 'wrong'},
                   {'snapshot': {**snapshot(), 'url': URL.replace('123', '456')}},
                   {'snapshot': {**snapshot(), 'session_id': 'wrong'}},
                   {'expected_revision': 1},
                   {'captured_at': (datetime.now(timezone.utc)-timedelta(minutes=6)).isoformat()}):
        with pytest.raises((Conflict, NotFound)):
            capture(v2_db, d2_run, **change)
    assert repo.get_run(owner, run['id'])['run_revision'] == 2


def test_worker_graph_produces_report_and_zero_browser_actions(v2_db, d2_run, monkeypatch):
    repo, owner, run, _ = d2_run
    repo.save_fact(owner, semantic_key='name', subject_id='person', scope_key='global', value='SYNTHETIC-VERIFIED-NAME')
    observed, _ = capture(v2_db, d2_run)
    monkeypatch.setenv('ZHIDA_V2_WORKFLOW_ENABLED', 'true')
    monkeypatch.setenv('ZHIDA_V2_RECOGNITION_ENABLED', 'true')
    with v2_db.transaction(owner) as cx:
        event = cx.execute(text('SELECT current_event_id FROM zhida.background_jobs WHERE id=:id'),
                           {'id': observed['job_id']}).scalar_one()
    worker = WorkerDispatcher(v2_db, engine=_configured_engine(v2_db))
    worker.dispatch(event_id=event, user_id=owner)
    worker.dispatch(event_id=event, user_id=owner)
    report = ObservationStore(v2_db).report(owner, run['id'])
    assert not report['stale']
    data = report['analysis']['report']
    assert data['plan']['suggestions'][0]['value'] == 'SYNTHETIC-VERIFIED-NAME'
    assert data['plan']['executable_actions'] == [] and data['browser_actions_executed'] == 0
    assert not data['application_complete']
    with v2_db.transaction(owner) as cx:
        assert cx.execute(text('SELECT count(*) FROM zhida.action_ledger WHERE run_id=:r'), {'r': run['id']}).scalar() == 0
        assert cx.execute(text('SELECT count(*) FROM zhida.form_analysis_reports WHERE run_id=:r'), {'r': run['id']}).scalar() == 1
        checkpoint = cx.execute(text('SELECT state FROM zhida.workflow_checkpoints WHERE run_id=:r'), {'r': run['id']}).scalar_one()
        assert checkpoint['trace'] == ['bind_context','validate_context','recognize_form','retrieve_evidence','plan_answers','audit_coverage']
        assert 'SYNTHETIC-VERIFIED-NAME' not in encode(checkpoint)
        assert 'SYNTHETIC-VERIFIED-NAME' not in encode([e['payload'] for e in repo.events(owner, run['id'])])


def test_confirmation_is_saved_and_reused_without_waiting_for_website_write(v2_db, d2_run):
    repo, owner, run, selected = d2_run
    retriever = SyntheticRetriever()
    capture(v2_db, d2_run)
    result = advance(v2_db, d2_run, retriever=retriever)
    assert result.state == 'awaiting_user'
    current = repo.get_run(owner, run['id'])
    question = current['questions'][0]
    assert question['label'] == '姓名'
    assert retriever.calls[0][2]['resume_revision_id'] == selected['revision_id']
    repo.answer(owner, run['id'], question['id'], value='SYNTHETIC-CONFIRMED',
                expected_revision=current['run_revision'], expected_question_revision=question['question_revision'])
    assert repo.facts(owner)[0]['value'] == 'SYNTHETIC-CONFIRMED'
    result = advance(v2_db, d2_run)
    assert result.state == 'awaiting_browser' and result.result['reason'] == 'analysis_complete_read_only'
    report = ObservationStore(v2_db).report(owner, run['id'])['analysis']['report']
    assert report['plan']['suggestions'][0]['value'] == 'SYNTHETIC-CONFIRMED'
    assert not repo.get_run(owner, run['id'])['questions']


def test_incomplete_question_or_dropdown_is_system_issue_not_blind_user_question(v2_db, d2_run):
    ambiguous = raw_field('是', question_candidates=[], question_text='', control_group_key='',
                          field_type='radio', control_kind='radio', option_label='是')
    missing = raw_field('意向工作城市', selector='#city', field_type='combobox', control_kind='custom_select',
                        role='combobox', options=[], observation={'question_status':'verified','options_status':'unavailable'})
    capture(v2_db, d2_run, raw=snapshot(ambiguous, missing))
    result = advance(v2_db, d2_run)
    repo, owner, run, _ = d2_run
    report = ObservationStore(v2_db).report(owner, run['id'])['analysis']['report']
    assert result.state == 'awaiting_browser'
    assert not report['plan']['questions'] and report['recognition']['needs_observation']


@pytest.mark.parametrize('change', ['fact', 'epoch', 'cancel', 'new_capture'])
def test_changes_during_analysis_cannot_commit_a_stale_report(v2_db, d2_run, change):
    repo, owner, run, _ = d2_run
    capture(v2_db, d2_run)
    result = advance(v2_db, d2_run, commit=False)
    if change == 'fact':
        repo.save_fact(owner, semantic_key='name', subject_id='person', scope_key='global', value='new')
    elif change == 'epoch':
        with v2_db.transaction(owner) as cx:
            cx.execute(text('UPDATE zhida.browser_sessions SET connection_epoch=2 WHERE id=:id'), {'id': run['browser_session_id']})
    elif change == 'cancel':
        repo.cancel_run(owner, run['id'], repo.get_run(owner, run['id'])['run_revision'])
    else:
        capture(v2_db, d2_run)
    with pytest.raises(StaleWork), v2_db.transaction(owner) as cx:
        result.effects(cx)
    assert ObservationStore(v2_db).report(owner, run['id'])['analysis'] is None


def test_report_checkpoint_and_questions_rollback_together(v2_db, d2_run):
    repo, owner, run, _ = d2_run
    capture(v2_db, d2_run)
    result = advance(v2_db, d2_run, commit=False)
    with pytest.raises(RuntimeError), v2_db.transaction(owner) as cx:
        result.effects(cx)
        raise RuntimeError('synthetic interruption')
    assert ObservationStore(v2_db).report(owner, run['id'])['analysis'] is None
    assert not repo.get_run(owner, run['id'])['questions']
    assert repo.get_run(owner, run['id'])['run_revision'] == 2


def test_expired_observation_waits_without_model_browser_or_report(v2_db, d2_run):
    _, owner, run, _ = d2_run
    capture(v2_db, d2_run)
    result = ReadOnlyWorkflowEngine(v2_db, retriever=SyntheticRetriever(),
        clock=lambda: datetime.now(timezone.utc)+timedelta(minutes=6)).advance(user_id=owner, run_id=run['id'], expected_revision=2)
    assert result.result['reason'] == 'observation_expired'
    assert not result.result['analysis_available'] and result.result['model_calls_executed'] == 0


def test_native_api_is_explicitly_gated_and_owner_scoped(v2_db, d2_run):
    _, owner, run, _ = d2_run
    cfg = Settings(database_url=str(v2_db.engine.url), local_user_enabled=True, local_user_id=owner,
                   workflow_enabled=True, recognition_enabled=True)
    with TestClient(create_app(cfg, db=v2_db, control=AllowedControl()), client=('127.0.0.1',12345)) as client:
        assert client.get('/api/v2/health').json()['workflow'] == D2_VERSION
        _, inputs = capture(v2_db, d2_run)
        response = client.post(f"/api/v2/runs/{run['id']}/observations", json=inputs)
        assert response.status_code == 202 and response.json()['duplicate']
        assert client.get(f"/api/v2/runs/{run['id']}/analysis").status_code == 200
        assert client.get('/api/v2/runs/foreign-run/analysis').status_code == 404
    with TestClient(create_app(Settings(database_url=str(v2_db.engine.url), local_user_enabled=True, local_user_id=owner),
                              db=v2_db, control=AllowedControl()), client=('127.0.0.1',12345)) as client:
        assert client.post(f"/api/v2/runs/{run['id']}/observations", json=inputs).status_code == 409
    with pytest.raises(ValueError, match='WORKFLOW_ENABLED'), TestClient(create_app(
            Settings(database_url=str(v2_db.engine.url), recognition_enabled=True), db=v2_db)):
        pass


def test_d1_pinned_tasks_keep_d1_engine_when_d2_enabled(v2_db, monkeypatch):
    monkeypatch.setenv('ZHIDA_V2_WORKFLOW_ENABLED','true')
    monkeypatch.setenv('ZHIDA_V2_RECOGNITION_ENABLED','true')
    engines = _configured_engine(v2_db)
    assert engines.for_version('langgraph-d1-v1').workflow_version == 'langgraph-d1-v1'
    assert engines.for_version(D2_VERSION).workflow_version == D2_VERSION


def test_checkbox_confirmation_uses_actual_options_and_remembers_list(v2_db, d2_run):
    repo, owner, run, _ = d2_run
    options = ['招聘网站', '公众号']
    fields = [raw_field('招聘信息来源', selector='#source-' + str(i), field_type='checkbox',
                        control_kind='checkbox', control_group_key='source-group', option_label=option,
                        option_value=str(i), options=options, options_capture='group_complete')
              for i, option in enumerate(options)]
    capture(v2_db, d2_run, raw=snapshot(*fields))
    advance(v2_db, d2_run)
    current = repo.get_run(owner, run['id'])
    question = current['questions'][0]
    assert question['control_type'] == 'checkbox' and question['expected_fact_revision'] == 0
    args = dict(expected_revision=current['run_revision'], expected_question_revision=question['question_revision'])
    with pytest.raises(ValueError):
        repo.answer(owner, run['id'], question['id'], value=['不存在的选项'], **args)
    assert repo.facts(owner) == []
    repo.answer(owner, run['id'], question['id'], value=options, **args)
    advance(v2_db, d2_run)
    plan = ObservationStore(v2_db).report(owner, run['id'])['analysis']['report']['plan']
    assert plan['suggestions'][0]['value'] == options and plan['executable_actions'] == []


def test_date_confirmation_validates_calendar_before_creating_memory(v2_db, d2_run):
    repo, owner, run, _ = d2_run
    capture(v2_db, d2_run, raw=snapshot(raw_field('出生日期', field_type='date', control_kind='date', date_precision='date')))
    advance(v2_db, d2_run)
    current = repo.get_run(owner, run['id'])
    question = current['questions'][0]
    assert question['input_evidence']['date_precision'] == 'date'
    args = dict(expected_revision=current['run_revision'], expected_question_revision=question['question_revision'])
    for value in ('2000-02', '2000-02-31', {'date': '2000-02-01'}):
        with pytest.raises(ValueError):
            repo.answer(owner, run['id'], question['id'], value=value, **args)
    assert repo.facts(owner) == []
    repo.answer(owner, run['id'], question['id'], value='2000-02-01', **args)
    assert repo.facts(owner)[0]['value'] == '2000-02-01'
