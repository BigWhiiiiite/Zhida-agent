"""Upload intent regression; anonymous in-memory Row, no credentials/files/browser."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.browser_models import ExecutePlanRequest, ExecutionResult, PreSubmitCheck


SESSION = 'execute-upload-fixture'
RESUME = 'bound-fixture'
CONTEXT = 'current-fixture-revision'
UPLOADS = Path('/private/tmp/zhida-test-upload-intent-not-accessed')


def run() -> None:
    assert ExecutePlanRequest(actions=[],resume_id=RESUME).upload_resume is False
    # Use the real storage return type: dict mocks hid a live .get() failure.
    with sqlite3.connect(':memory:') as fixture_db:
        fixture_db.row_factory=sqlite3.Row
        fixture_row=fixture_db.execute(
            'select ? as stored_filename, ? as filename',
            ('fixture.pdf','匿名求职简历.pdf')).fetchone()
        nameless_row=fixture_db.execute(
            'select ? as stored_filename, ? as filename',('fixture.pdf','')).fetchone()
    # Patch before importing app.main: its module-level load_dotenv must never
    # read local credentials, and no accidental storage path may be reached.
    with patch('dotenv.load_dotenv'), patch('sqlite3.connect',side_effect=AssertionError('Database forbidden')):
        from app import main
        result = ExecutionResult(url='https://fixture.example.test/form',completed=0,skipped=0,failed=0,
            results=[],pre_submit=PreSubmitCheck(url='https://fixture.example.test/form'))
        fixture_app = FastAPI()
        fixture_app.add_api_route('/api/browser/{session_id}/execute',main.execute_form_plan,
                                 methods=['POST'],response_model=ExecutionResult)
        with patch.object(main,'current_user_id',return_value='fixture-user'), \
             patch.dict(main.browser_session_owners,{SESSION:'fixture-user'},clear=True), \
             patch.dict(main.browser_task_resumes,{SESSION:RESUME},clear=True), \
             patch.object(main,'_task_context',return_value=(None,CONTEXT)), \
             patch.object(main,'_require_application_form',AsyncMock()) as form_gate, \
             patch.object(main,'get_resume_internal') as lookup, \
             patch.object(main,'UPLOAD_DIR',UPLOADS), \
             patch.object(main.browser_demo,'execute',AsyncMock(return_value=result)) as execute, \
             patch.object(main.browser_demo,'import_resume_with_site_parser',AsyncMock()) as site_parser, \
             TestClient(fixture_app) as client:
            body={'actions':[],'resume_id':RESUME,'context_token':CONTEXT}
            endpoint=f'/api/browser/{SESSION}/execute'
            for extra in ({},{'upload_resume':False}):
                execute.reset_mock();lookup.reset_mock()
                response=client.post(endpoint,json={**body,**extra})
                assert response.status_code==200,response.text
                lookup.assert_not_called()
                assert execute.await_args.args[2] is None
                assert execute.await_args.args[1].resume_id==RESUME
                assert execute.await_args.args[1].upload_resume is False

            # Only an explicit true intent resolves the original attachment and
            # supplies a path to the browser's upload branch. No files are read.
            execute.reset_mock();lookup.reset_mock()
            lookup.return_value=fixture_row
            with patch.object(Path,'exists',return_value=True) as exists:
                response=client.post(endpoint,json={**body,'upload_resume':True})
            assert response.status_code==200,response.text
            lookup.assert_called_once_with(RESUME)
            exists.assert_called_once()
            assert execute.await_args.args[2]==UPLOADS/'fixture.pdf'
            assert execute.await_args.kwargs['resume_filename']=='匿名求职简历.pdf'
            assert execute.await_args.args[1].upload_resume is True

            execute.reset_mock();lookup.return_value=nameless_row
            with patch.object(Path,'exists',return_value=True):
                response=client.post(endpoint,json={**body,'upload_resume':True})
            assert response.status_code==200,response.text
            assert execute.await_args.kwargs['resume_filename']=='fixture.pdf'
            lookup.return_value=fixture_row

            # Supplying a bound resume alone still works if its attachment file
            # has gone missing; explicit attachment upload must fail closed.
            execute.reset_mock();lookup.reset_mock()
            with patch.object(Path,'exists',side_effect=AssertionError('No file check without upload intent')):
                assert client.post(endpoint,json=body).status_code==200
            lookup.assert_not_called()
            execute.reset_mock()
            with patch.object(Path,'exists',return_value=False):
                response=client.post(endpoint,json={**body,'upload_resume':True})
            assert response.status_code==404 and '原始文件不存在' in response.text
            execute.assert_not_awaited()

            execute.reset_mock();lookup.reset_mock()
            lookup.return_value=None
            response=client.post(endpoint,json={**body,'upload_resume':True})
            assert response.status_code==404 and '选择的简历不存在' in response.text
            execute.assert_not_awaited()
            for invalid in ({'resume_id':'different-fixture'},{'context_token':'stale'}):
                lookup.reset_mock();execute.reset_mock()
                response=client.post(endpoint,json={**body,'upload_resume':True,**invalid})
                assert response.status_code==409,response.text
                lookup.assert_not_called();execute.assert_not_awaited()
            assert form_gate.await_count>=5
            site_parser.assert_not_awaited()
    print('execute_resume_upload_test: OK (real anonymous SQLite Row, opt-in, original filename, missing/stale guards, no live DB/env/browser)')


if __name__=='__main__':
    run()
