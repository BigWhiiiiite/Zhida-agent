"""HTTP capability/start boundary; no real user, browser or model."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import FastAPI,HTTPException
from fastapi.testclient import TestClient
from app import live_assisted as live


def client(monkeypatch,*,enabled=True,context=('resume','ctx'),owned=True,quota=True):
    calls=[]
    class Store:
        def start(self,*args):calls.append('start');return {'run_id':args[1],'status':'running','events':[]},False
    def owner(s):
        if not owned:raise HTTPException(403,'not owner')
    def control(u):
        if not quota:raise ValueError('quota')
    monkeypatch.setattr(live,'enabled',lambda:enabled)
    monkeypatch.setattr(live,'store',lambda:Store())
    monkeypatch.setattr(live,'current_user_id',lambda:'synthetic')
    monkeypatch.setattr(live,'require_write_control',control)
    service=SimpleNamespace(context=SimpleNamespace(),_require=lambda s:SimpleNamespace(url='https://jobs.synthetic.invalid/form'),
        _validate_url=lambda url:url)
    app=FastAPI();lock=asyncio.Lock()
    app.include_router(live.create_assisted_router(service=service,require_owner=owner,task_binding=lambda s:context,
        require_local=lambda r:None,prepare=None,operation_lock=lambda:lock))
    return TestClient(app),calls


def test_start_returns_short_202_idempotent_receipt_not_a_browser_result(monkeypatch):
    http,calls=client(monkeypatch)
    response=http.post('/api/browser/s/assist/start?run_id=round-a',json={'resume_id':'resume'})
    assert response.status_code==202 and response.json()['status']=='running'
    assert 'snapshot' not in response.json() and calls==['start']


@pytest.mark.parametrize('options,status',[({'enabled':False},409),({'context':('other','ctx')},409),
    ({'owned':False},403),({'quota':False},503)])
def test_disabled_wrong_resume_owner_and_quota_never_dispatch(monkeypatch,options,status):
    http,calls=client(monkeypatch,**options)
    assert http.post('/api/browser/s/assist/start?run_id=round-a',json={'resume_id':'resume'}).status_code==status
    assert calls==[]


def test_caller_cannot_supply_untrusted_browser_evidence(monkeypatch):
    http,calls=client(monkeypatch)
    assert http.post('/api/browser/s/assist/start?run_id=round-a',json={'resume_id':'resume','snapshot':{'fields':[]}}).status_code==422
    assert calls==[]


def test_background_task_error_is_consumed_without_logging_private_error(caplog):
    class Task:
        def cancelled(self):return False
        def exception(self):return ValueError('PRIVATE-SYNTHETIC-APPLICANT')
    task=Task();live.TASKS.add(task)
    live._task_finished(task)
    assert task not in live.TASKS
    assert 'ValueError' in caplog.text
    assert 'PRIVATE-SYNTHETIC-APPLICANT' not in caplog.text
