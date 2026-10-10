"""Isolated real endpoint/middleware: no employer/browser/model/DB writes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import FastAPI

from app.browser_models import BrowserSnapshot, PageField
from app.form_observation import build_report


async def run():
    with patch('dotenv.load_dotenv'), patch('sqlite3.connect', side_effect=AssertionError('No DB')):
        from app import main
        fixture = FastAPI()
        fixture.middleware('http')(main.require_account)
        fixture.add_api_route('/api/browser/{session_id}/extraction-audit', main.extraction_audit, methods=['POST'])
        main.app.state.browser_operation_lock = asyncio.Lock()
        snapshot = BrowserSnapshot(session_id='sid', url='https://fixture.example.test/form', title='Anonymous',
            fields=[PageField(selector='#name', label='姓名', question_text='姓名', label_source='explicit',
                              current_value='unchanged')])
        fresh = AsyncMock(return_value=snapshot)
        execute = AsyncMock(side_effect=AssertionError('No writes'))
        controls = AsyncMock(side_effect=AssertionError('No implicit clicks'))
        regions = AsyncMock(side_effect=AssertionError('No implicit images'))
        with patch.object(main, 'user_for_token', return_value=SimpleNamespace(id='owner')), \
             patch.dict(main.browser_session_owners, {'sid':'owner'}, clear=True), \
             patch.dict(main.browser_image_consents, {}, clear=True), \
             patch.object(main, '_task_context', return_value=(None, 'x'*64)), \
             patch.object(main.browser_demo, 'snapshot_for', fresh), \
             patch.object(main.browser_demo, 'execute', execute), \
             patch.object(main.browser_demo, 'observe_form_controls', controls), \
             patch.object(main.browser_demo, 'observe_page_region', regions), \
             patch.object(main, '_model_task_plan', side_effect=AssertionError('No fill plans')):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture), base_url='http://127.0.0.1') as client:
                url = '/api/browser/sid/extraction-audit'
                payload = {'context_token':'x'*64, 'use_model':False}
                response = await client.post(url, json=payload)
                assert response.status_code == 200, response.text
                data = response.json()
                assert data['read_only'] and data['model_status'] == 'not_requested'
                assert 'unchanged' not in response.text and 'actions' not in data
                execute.assert_not_awaited(); controls.assert_not_awaited(); regions.assert_not_awaited()
                with patch.object(main, 'user_for_token', return_value=SimpleNamespace(id='other')):
                    assert (await client.post(url, json=payload)).status_code == 404
                assert (await client.post(url, json={**payload, 'context_token':'z'*64})).status_code == 409
                assert (await client.post(url, json={**payload, 'include_images':True})).status_code == 409
                assert (await client.post(url, json={**payload, 'fill':True})).status_code == 422
                # Changed form answers at the final read invalidate the result.
                changed = snapshot.model_copy(deep=True)
                changed.fields[0].current_value = 'other answer'
                fresh.side_effect = [snapshot, changed]
                assert (await client.post(url, json=payload)).status_code == 409
                execute.assert_not_awaited()
                # Visible questions can stay identical while a collapsed or
                # embedded surface changes; do not reuse the old scope report.
                baseline = snapshot.model_copy(deep=True)
                baseline.extraction_report = build_report(baseline.fields, {
                    'observed_controls': 1, 'captured_controls': 1})
                changed = baseline.model_copy(deep=True)
                changed.extraction_report.pending_sections = ['教育经历']
                changed.extraction_report.unread_shadow_regions = 1
                fresh.side_effect = [baseline, changed]
                assert (await client.post(url, json=payload)).status_code == 409
                execute.assert_not_awaited()
    print('extraction_audit_api_test: OK (ownership, consent, zero writes, stale read invalidation)')


if __name__ == '__main__':
    asyncio.run(run())
