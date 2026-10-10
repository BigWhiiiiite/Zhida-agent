"""Live progress under the real middleware lock; isolated ASGI, no browser/DB."""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch

import httpx
from fastapi import FastAPI

from app.browser_models import ApplicationAssistEvent, ApplicationAssistResult, BrowserSnapshot
from app.assist_runs import AssistRuns, safe_interrupt_reason


async def run():
    with patch('dotenv.load_dotenv'), patch('sqlite3.connect',side_effect=AssertionError('No DB')):
        from app import main
        fixture=FastAPI()
        fixture.middleware('http')(main.require_account)
        fixture.add_api_route('/api/browser/{session_id}/assist',main.assist_application,methods=['POST'])
        fixture.add_api_route('/api/browser/{session_id}/assist/progress/{run_id}',main.assist_progress,methods=['GET'])
        fixture.add_api_route('/api/browser/{session_id}/assist/progress/{run_id}/cancel',main.cancel_assist,methods=['POST'])
        main.app.state.browser_operation_lock=asyncio.Lock()
        started=asyncio.Event();release=asyncio.Event();writes=[]
        async def prepare(sid,payload,*,on_progress,cancelled):
            on_progress(ApplicationAssistEvent(kind='model',message='合成分析中'))
            started.set();await release.wait()
            if not cancelled():writes.append('write')
            return ApplicationAssistResult(status='needs_user',message='合成结束',
                snapshot=BrowserSnapshot(session_id=sid,url='https://fixture.test/form',title='合成',fields=[]))
        with patch.object(main,'assist_runs',AssistRuns()), \
             patch.object(main,'user_for_token',return_value=SimpleNamespace(id='owner')), \
             patch.dict(main.browser_session_owners,{'sid':'owner'},clear=True), \
             patch.object(main,'_require_task_resume'), \
             patch.object(main,'_prepare_application_assist',side_effect=prepare) as engine, \
             nullcontext(httpx.AsyncClient(transport=httpx.ASGITransport(app=fixture),base_url='http://127.0.0.1')) as client:
            endpoint='/api/browser/sid/assist'
            pending=asyncio.create_task(client.post(endpoint+'?run_id=run',json={'resume_id':'cv'}))
            await asyncio.wait_for(started.wait(),2)
            progress=await asyncio.wait_for(client.get(endpoint+'/progress/run'),2)
            assert progress.status_code==200 and progress.json()['phase']=='model',progress.text
            response=await asyncio.wait_for(client.post(endpoint+'/progress/run/cancel'),2)
            assert response.json()['cancel_requested'] is True
            with patch.object(main,'user_for_token',return_value=SimpleNamespace(id='other')):
                assert (await client.get(endpoint+'/progress/run')).status_code==404
            release.set()
            response=await pending
            assert response.status_code==200 and not writes,(response.text,writes)
            # A resent request with the same id returns the receipt, not execution.
            assert (await client.post(endpoint+'?run_id=run',json={'resume_id':'cv'})).status_code==200
            assert engine.call_count==1
            assert (await client.post(endpoint+'?run_id=run',json={'resume_id':'other'})).status_code==409
            assert (await client.get(endpoint+'/progress/missing')).status_code==404
            async def failed_prepare(sid,payload,*,on_progress,cancelled):
                on_progress(ApplicationAssistEvent(kind='observe',message='匿名读取中'))
                raise ValueError('申请表仍在加载或选项尚未稳定，请稍后重试；本次未执行填写')
            with patch.object(main,'_prepare_application_assist',side_effect=failed_prepare):
                response=await client.post(endpoint+'?run_id=failed',json={'resume_id':'cv'})
                assert response.status_code==409
                failure=(await client.get(endpoint+'/progress/failed')).json()
                assert failure['status']=='interrupted' and '结构的等待时间' in failure['message'],failure
            assert 'secret' not in safe_interrupt_reason(RuntimeError('secret provider data'))
            assert 'secret' not in safe_interrupt_reason(ValueError('secret field value'))
            await client.aclose()
    print('assist_progress_api_test: OK (progress/cancel bypass browser lock, owner scope, no replay)')


if __name__=='__main__':asyncio.run(run())
