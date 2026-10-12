"""Reservation path verified without any paid/external model request."""
import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.backend_v2 import model_budget as budget
from app.backend_v2.repository import Repository
from app.model_provider import MeteredResponsesModel, OpenAIResponsesModel


class Control:
    def __init__(self,allowed=True):self.calls=[];self.allowed=allowed
    def reserve_provider(self,**kw):self.calls.append(('reserve',kw));return SimpleNamespace(allowed=self.allowed)
    def mark_provider_sent(self,**kw):self.calls.append(('sent',kw));return True
    def settle_provider(self,**kw):self.calls.append(('settle',kw));return True


@pytest.mark.parametrize('tokens',[300,None])
def test_database_usage_and_shared_reservation_are_recorded(v2_db,monkeypatch,tokens):
    control=Control();monkeypatch.setattr(budget,'control',lambda:control);monkeypatch.setattr(budget,'database',lambda:v2_db)
    user='model-d3-'+uuid4().hex;Repository(v2_db).ensure_user(user)
    reservation=budget.ModelReservation({'user_id':user,'run_id':None},'synthetic-model','PRIVATE-SYNTHETIC-INPUT')
    reservation.start();reservation.finish(SimpleNamespace(total_tokens=tokens) if tokens is not None else None)
    with v2_db.transaction(user) as cx:
        row=cx.execute(text('SELECT * FROM zhida.model_calls WHERE user_id=:u'),{'u':user}).mappings().one()
        assert row['status']==('completed' if tokens is not None else 'unknown')
        assert 'PRIVATE-SYNTHETIC-INPUT' not in str(dict(row))
    assert [c[0] for c in control.calls]==['reserve','sent','settle']
    assert control.calls[-1][1]['state']==('completed' if tokens is not None else 'unknown')


def test_refused_reservation_makes_no_provider_request(monkeypatch):
    control=Control(False);monkeypatch.setattr(budget,'control',lambda:control)
    async def unexpected(self,*a,**kw):raise AssertionError('Quota rejection must precede HTTP')
    monkeypatch.setattr(OpenAIResponsesModel,'get_response',unexpected)
    model=MeteredResponsesModel(model='synthetic',openai_client=object())
    token=budget.MODEL_SCOPE.set({'user_id':'synthetic','run_id':None})
    try:
        with pytest.raises(ValueError):asyncio.run(model.get_response(input='synthetic'))
    finally:budget.MODEL_SCOPE.reset(token)
    assert [c[0] for c in control.calls]==['reserve']


def test_legacy_non_d3_model_path_is_not_silently_metered(monkeypatch):
    calls=[]
    async def fake(self,*a,**kw):calls.append('mock-only');return 'synthetic-response'
    monkeypatch.setattr(OpenAIResponsesModel,'get_response',fake)
    model=MeteredResponsesModel(model='synthetic',openai_client=object())
    assert asyncio.run(model.get_response(input='synthetic'))=='synthetic-response'
    assert calls==['mock-only']


def test_d3_disables_hidden_http_retries_without_mutating_shared_client(monkeypatch):
    calls=[]
    class Client:
        def __init__(self,retries):self.max_retries=retries
        def with_options(self,**options):
            calls.append(('client',options))
            return Client(options['max_retries'])
    class Reservation:
        def __init__(self,*args):pass
        def start(self):calls.append('reserve')
        def finish(self,usage=None):calls.append('settle')
    async def fake(self,*args,**kwargs):
        assert self._client.max_retries==0
        calls.append('single-attempt')
        return SimpleNamespace(usage=SimpleNamespace(total_tokens=3))
    monkeypatch.setattr(budget,'ModelReservation',Reservation)
    monkeypatch.setattr(OpenAIResponsesModel,'get_response',fake)
    original=Client(6)
    model=MeteredResponsesModel(model='synthetic',openai_client=original)
    token=budget.MODEL_SCOPE.set({'user_id':'synthetic','run_id':None})
    try:asyncio.run(model.get_response(input='synthetic'))
    finally:budget.MODEL_SCOPE.reset(token)
    assert original.max_retries==6
    assert calls==['reserve',('client',{'max_retries':0}),'single-attempt','settle']
