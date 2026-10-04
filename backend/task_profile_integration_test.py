"""Isolated task/resume/memory/RAG regressions. No real profile or model calls."""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app import main, storage
from app.application_knowledge import company_scope
from app.application_models import ApplicationWorkflowState
from app.browser_models import BrowserSnapshot, PageField
from app.form_evidence import retrieve_form_evidence
from app.form_agent import _saved_answer_match
from app.job_rag import split_profile_evidence
from app.models import CandidateProfile, Education, Project, ResumeProfile, ResumeRecord, FieldEvidence, ApplicationAnswerMemory
from app.profile_service import save_application_answer
from app.task_profile import compose_task_profile, context_token


URL = "https://app.mokahr.com/campus-recruitment/fixture/1#/apply"


def record(identifier: str, project: str) -> ResumeRecord:
    return ResumeRecord(id=identifier, filename=identifier + '.pdf', label=identifier,
        parser='fixture', status='completed', created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc), profile=ResumeProfile(
            name='旧简历姓名', projects=[Project(name=project, description='开发 Agent 工具调用服务。')],
            education=[Education(school='本科大学', degree='本科', college='本科软件学院'),
                       Education(school='研究生大学', degree='硕士', college='研究生计算学院')]))


def run() -> None:
    master = CandidateProfile(name='已确认姓名', phone='123', projects=[Project(name='其他简历项目')])
    a, b = record('version-a', 'Agent 工具调用'), record('version-b', '搜索推荐')
    first = compose_task_profile(master, a)
    assert first.name == master.name and first.phone == master.phone
    assert [p.name for p in first.projects] == ['Agent 工具调用']
    assert first.education[0].college == '本科软件学院'
    assert first.education[1].college == '研究生计算学院'
    first.application_answer_memory = [ApplicationAnswerMemory(id='college', question='学院',
        normalized_question='学院', semantic_key='education.college', entity_scope='education:bachelor',
        field_signature='shared', value='本科软件学院', updated_at=datetime.now(timezone.utc))]
    assert not _saved_answer_match(PageField(selector='#college', label='硕士学院',
        semantic_key='education.college', entity_scope='education:master', field_signature='shared'), first).value
    assert context_token(first, a) != context_token(compose_task_profile(master, b), b)
    empty = b.model_copy(deep=True)
    empty.profile.projects = []
    assert compose_task_profile(master, empty).projects == []
    unreviewed = a.model_copy(deep=True)
    unreviewed.evidence = [FieldEvidence(id='pending', field_path='projects', value=[], confidence=.5,
                                       source_text='待核对', status='pending_review')]
    assert not compose_task_profile(master, unreviewed).projects
    with TemporaryDirectory() as directory, \
            patch.object(storage, 'DATA_DIR', Path(directory)), \
            patch.object(storage, 'DB_PATH', Path(directory) / 'test.db'), \
            patch.object(main, 'UPLOAD_DIR', Path(directory) / 'uploads'), \
            patch.dict(os.environ, {'APP_AUTH_REQUIRED': 'false'}), \
            patch.object(main.browser_demo, 'page', SimpleNamespace(url=URL)), \
            patch.dict(main.browser_task_resumes, {}, clear=True), \
            patch.dict(main.browser_session_owners, {}, clear=True), \
            patch.object(main.browser_demo, 'close', new_callable=AsyncMock), \
            TestClient(main.app) as client:
        user = client.get('/api/auth/me').json()['id']
        token = storage.set_current_user(user)
        try:
            storage.save_profile(master)
            for item in (a, b):
                storage.create_pending_resume(item.id, item.filename, item.filename, item.label,
                    'fixture', '中文', 0, item.id, '')
                storage.replace_parse_result(item.id, item.profile, 'fixture', [], '')
            save_application_answer('业务组偏好', '', '基础平台', resume_id=a.id, source_url=URL,
                                    field_signature='fixture-question')
            saved = storage.get_profile()
            scoped = compose_task_profile(saved, a, company_scope(URL))
            assert len(scoped.application_answer_memory) == 1
            assert not compose_task_profile(saved, b, company_scope(URL)).application_answer_memory
            other_url = URL.replace('/fixture/', '/other-company/')
            assert not compose_task_profile(saved, a, company_scope(other_url)).application_answer_memory
            assert company_scope(URL) == company_scope(URL.replace('/campus-recruitment/', '/apply/'))
            save_application_answer('QQ号', '', '123456', resume_id=a.id, source_url=URL, semantic_key='person.qq')
            assert storage.get_profile().qq == '123456'
            cards = split_profile_evidence(scoped)
            snapshot = BrowserSnapshot(session_id='s', url=URL, title='申请', fields=[
                PageField(selector='#project', label='Agent 工具调用项目', semantic_key='project.description')])
            with patch('app.form_evidence._indexed_chunks', return_value=(cards, False)):
                evidence = retrieve_form_evidence(snapshot, scoped)
            assert evidence['mode'] == 'keyword-only' and evidence['matches']
            assert all(m['source_title'] == 'Agent 工具调用' and m['requires_grounding'] for m in evidence['matches'])
            assert '搜索推荐' not in str(evidence) and '其他简历项目' not in str(evidence)
            with patch('app.form_evidence._indexed_chunks', return_value=(
                    [replace(card, vector=(1.0, 0.0)) for card in cards], True)), \
                    patch('app.form_evidence._embed', return_value=[(1.0, 0.0)]):
                hybrid = retrieve_form_evidence(snapshot, scoped)
            assert hybrid['mode'] == 'hybrid' and hybrid['matches'][0]['score'] > 0

            main.browser_session_owners['s'] = user
            with patch.object(main.browser_demo, 'snapshot_for', new_callable=AsyncMock, return_value=snapshot), \
                    patch.object(main.browser_demo, 'workflow_state', new_callable=AsyncMock, return_value=
                        ApplicationWorkflowState(session_id='s', url=URL, title='申请', stage='application_form', form_fields=1)), \
                    patch.object(main.browser_demo, 'execute', new_callable=AsyncMock) as execute:
                assert client.put('/api/browser/s/resume', json={'resume_id': a.id}).status_code == 200
                review = client.post('/api/browser/s/review')
                assert review.status_code == 200, review.text
                plan = review.json()['plan']
                assert plan['resume_id'] == a.id and plan['context_token']
                assert client.put('/api/browser/s/resume', json={'resume_id': b.id}).status_code == 200
                stale = client.post('/api/browser/s/execute', json={'actions': [], 'resume_id': b.id,
                                    'context_token': plan['context_token']})
                assert stale.status_code == 409, stale.text
                assert execute.await_count == 0
                # Changing only the job URL must invalidate a plan as well.
                current_plan = client.post('/api/browser/s/review').json()['plan']
                main.browser_demo.page.url = URL.replace('#/apply', '#/job/different-job')
                changed_page = client.post('/api/browser/s/execute', json={'actions': [], 'resume_id': b.id,
                    'context_token': current_plan['context_token']})
                assert changed_page.status_code == 409 and execute.await_count == 0
                main.browser_demo.page.url = URL
                current_plan = client.post('/api/browser/s/review').json()['plan']
                changed = client.patch('/api/profile', json={'name': '修改后的本人姓名'})
                assert changed.status_code == 200
                changed_profile = client.post('/api/browser/s/execute', json={'actions': [], 'resume_id': b.id,
                    'context_token': current_plan['context_token']})
                assert changed_profile.status_code == 409 and execute.await_count == 0
                assert client.put('/api/browser/s/resume', json={'resume_id': 'absent'}).status_code == 404
                outsider = storage.set_current_user('other-user')
                try:
                    assert storage.get_resume(a.id) is None
                finally:
                    storage.reset_current_user(outsider)
        finally:
            storage.reset_current_user(token)
    print('task_profile_integration_test: OK (versions, education, tenant memory, retrieval, stale plan, ownership)')


if __name__ == '__main__':
    run()
