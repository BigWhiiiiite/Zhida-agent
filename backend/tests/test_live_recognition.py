"""Local preview boundary tests. No real browser, profile, model or database."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app import live_recognition as preview
from app.browser_models import BrowserSnapshot, PageField
from app.live_capture import CaptureError


URL = 'https://jobs.example.test/apply?job=1'


def snapshot():
    return BrowserSnapshot(session_id='session-a', url=URL, title='测试填写页', fields=[
        PageField(selector='#name', label='姓名', question_text='姓名', label_source='explicit',
                  field_type='text', current_value='不得回传的已填写值'),
        PageField(selector='#city', label='意向工作城市', question_text='意向工作城市',
                  label_source='explicit', field_type='combobox', options=[], options_capture='unavailable'),
    ])


def client(monkeypatch, *, owner=True, local=True, selected='resume-a', epoch='epoch-a', capture_error=None):
    calls, binding = [], [selected, epoch]

    def require_owner(session):
        if not owner or session != 'session-a':
            raise HTTPException(403, '不是你的招聘窗口')

    def require_local(request):
        if not local or request.headers.get('origin') != 'http://127.0.0.1:5173':
            raise HTTPException(403, '只允许本机工作台')

    async def capture(service, session, url, guard):
        guard()
        calls.append((session, url))
        if capture_error:
            raise capture_error
        return SimpleNamespace(snapshot=snapshot(), session_id=session, capture_id='capture-id',
            document_id='document-id', captured_at='2026-10-12T00:00:00+00:00', url=url)

    monkeypatch.setattr(preview, 'capture_bound_snapshot', capture)
    app = FastAPI()
    app.include_router(preview.recognition_preview_router(service=object(), require_owner=require_owner,
        task_binding=lambda session: tuple(binding), require_local=require_local))
    return TestClient(app), calls, binding


def post(http, **extra):
    return http.post('/api/browser/session-a/recognition-preview',
        headers={'origin': 'http://127.0.0.1:5173'},
        json={'expected_url': URL, 'resume_id': 'resume-a', **extra})


def test_live_preview_uses_d2_and_returns_no_fill_values_or_executable_plan(monkeypatch):
    http, calls, _ = client(monkeypatch)
    response = post(http)
    assert response.status_code == 200, response.text
    result = response.json()
    assert calls == [('session-a', URL)]
    assert result['recognition']['schema_version'] == 'form-recognition-v1'
    assert result['recognition']['fields'][0]['question'] == '姓名'
    assert result['recognition']['fields'][1]['matchable'] is False
    assert result['recognition']['executable_actions'] == []
    assert result['recognition']['ready_for_review'] is False
    assert result['recognition']['coverage']['application_complete'] is False
    assert result['graph_executed'] is result['persisted'] is False
    assert result['browser_actions_executed'] == result['model_calls_executed'] == 0
    assert 'current_value' not in response.text and '不得回传' not in response.text
    assert '#name' not in response.text


@pytest.mark.parametrize('settings', [{'owner': False}, {'local': False},
    {'selected': 'other-resume'}, {'epoch': ''}])
def test_local_owner_and_selected_resume_are_checked_before_browser_access(monkeypatch, settings):
    http, calls, _ = client(monkeypatch, **settings)
    assert post(http).status_code in {403, 409}
    assert calls == []


def test_client_cannot_supply_fake_snapshot_or_document_identity(monkeypatch):
    http, calls, _ = client(monkeypatch)
    assert post(http, snapshot={'fields': []}, document_id='fake').status_code == 422
    assert calls == []


def test_capture_failure_is_explicit_and_has_no_partial_result(monkeypatch):
    http, _, _ = client(monkeypatch, capture_error=CaptureError('document_changed', '页面已变化'))
    response = post(http)
    assert response.status_code == 409
    assert response.json() == {'detail': {'code': 'document_changed', 'message': '页面已变化'}}


def test_resume_switch_during_normalization_discards_preview(monkeypatch):
    http, _, binding = client(monkeypatch)
    original = preview.normalize_observation

    def normalize(value):
        result = original(value)
        binding[1] = 'new-epoch'
        return result

    monkeypatch.setattr(preview, 'normalize_observation', normalize)
    assert post(http).status_code == 409


def test_invalid_evidence_does_not_echo_private_validation_input(monkeypatch):
    http, _, _ = client(monkeypatch)

    def invalid(value):
        raise ValueError('private raw page credential')

    monkeypatch.setattr(preview, 'normalize_observation', invalid)
    response = post(http)
    assert response.status_code == 422
    assert 'private raw' not in response.text


def test_response_projection_excludes_anchor_values_and_dom_targets():
    normalized = {'schema_version': 'form-recognition-v1', 'issues': [], 'coverage': {},
        'needs_observation': True, 'read_only': True, 'ready_for_review': False, 'executable_actions': [],
        'fields': [{'id': 'f', 'question': '学院', 'control_kind': 'text',
                    'entity_binding': {'kind': 'education', 'status': 'record_observed',
                                       'anchors': [{'value': 'PRIVATE SCHOOL'}]},
                    'options': [{'label': '甲', 'value': 'internal-value', 'disabled': True}],
                    'current_value': 'PRIVATE VALUE', 'targets': [{'selector': '#private-selector'}]}]}
    result = preview.preview_projection(normalized)
    assert result['fields'][0]['entity_binding'] == {'kind': 'education', 'status': 'record_observed'}
    assert result['fields'][0]['options'] == [{'label': '甲', 'disabled': True}]
    assert 'PRIVATE' not in str(result) and 'internal-value' not in str(result)
