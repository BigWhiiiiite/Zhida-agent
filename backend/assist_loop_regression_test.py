"""Synthetic loop/receipts regression. No user files, model, DB or live site."""
import asyncio
from unittest.mock import patch

from app import application_assist as assist
from app.assist_runs import AssistRuns
from app.browser_models import ApplicationAssistRequest, FillAction, FormPlan, PageField
from app.models import CandidateProfile
from application_assist_test import Browser, URL


class DynamicBrowser(Browser):
    def __init__(self, count):
        super().__init__()
        self.count = count
        self.saved = {}
        self.values = {'name':'合成姓名','country':'中国','city':'北京'}

    async def settled_snapshot(self, sid):
        base = await super().settled_snapshot(sid)
        base.fields = [f for f in base.fields if f.selector == '#name']
        # Each selection reveals the next custom question. Previous controls
        # remain with their actual values, exactly as an ATS dependency does.
        for i in range(min(len(self.saved)+1,self.count)):
            base.fields.append(PageField(selector=f'#dynamic{i}', label=f'合成地点问题{i}',
                question_text=f'合成地点问题{i}', label_source='explicit', required=True,
                field_type='combobox', options=['北京'], current_value=self.saved.get(i,'')))
        return base

    async def execute(self, sid, request, before_action=None, on_progress=None):
        from app.browser_models import ActionResult, ExecutionResult
        self.calls.append(request)
        results = []
        for a in request.actions:
            before_action()
            self.saved[int(a.selector.removeprefix('#dynamic'))] = a.value
            results.append(ActionResult(selector=a.selector,label=a.label,status='filled',verified=True))
        return ExecutionResult(url=URL,completed=len(results),verified=len(results),failed=0,skipped=0,
                               results=results,pre_submit=await self.pre_submit_check(sid))


def local(snapshot, profile):
    actions=[FillAction(selector=f.selector,label=f.label,action='ask_user',needs_model=True,
                resolution_source='blocked') for f in snapshot.fields if f.selector != '#name']
    actions.append(FillAction(selector='#name',label='姓名',action='fill',value=profile.name,
                             confidence=1,value_source='主档案.name'))
    plan=FormPlan(url=snapshot.url,title=snapshot.title,actions=actions)
    plan.routing_summary.model_pending=len(actions)
    return plan


async def run():
    profile=CandidateProfile(name='合成姓名',location='北京')
    req=ApplicationAssistRequest(resume_id='synthetic',max_rounds=5)
    calls=[];events=[]
    async def model(snapshot):
        calls.append(snapshot)
        return FormPlan(url=URL,title='Fixture',actions=[
            FillAction(selector=f.selector,label=f.label,action='select',value=profile.location,
                confidence=1,resolution_source='model',value_source='主档案.location')
            for f in snapshot.fields if not f.current_value])
    with patch.object(assist,'create_local_form_plan',side_effect=local):
        browser=DynamicBrowser(2)
        result=await assist.prepare_application(browser,'fixture',req,profile,guard=lambda:None,
            model_plan=model,stamp_plan=lambda p:p,on_progress=events.append)
        assert result.status=='ready_for_review',result
        assert result.model_calls==2 and len(calls)==2 and len(browser.saved)==2
        assert [f.selector for f in calls[1].fields if not f.current_value]==['#dynamic1']
        assert {'observe','retrieve','decide','model','execute','verify'} <= {e.kind for e in events}
        browser=DynamicBrowser(3)
        result=await assist.prepare_application(browser,'fixture',req,profile,guard=lambda:None,
            model_plan=model,stamp_plan=lambda p:p)
        assert result.status=='partial' and result.model_calls==2 and len(browser.saved)==2
        # An unresolved question with unchanged real options is analysed once.
        count=[]
        async def undecided(snapshot):
            count.append(1);return local(snapshot,profile)
        browser=DynamicBrowser(1)
        result=await assist.prepare_application(browser,'fixture',req,profile,guard=lambda:None,
            model_plan=undecided,stamp_plan=lambda p:p)
        assert len(count)==1 and not browser.saved
        # Cancel after analysis but before the first value transmission.
        cancelled=[False]
        def guard():
            if cancelled[0]:raise ValueError('已暂停')
        def progress(e):
            if e.kind=='execute':cancelled[0]=True
        browser=DynamicBrowser(1)
        result=await assist.prepare_application(browser,'fixture',req,profile,guard=guard,
            model_plan=model,stamp_plan=lambda p:p,on_progress=progress)
        assert result.status=='blocked' and not browser.saved

    store=AssistRuns()
    p,replay=store.begin('run','owner','sid',req)
    assert not replay and p.status=='running'
    for owner,sid in [('other','sid'),('owner','other')]:
        try:store.get('run',owner,sid);raise AssertionError('Receipt leak')
        except LookupError:pass
    try:store.begin('run','owner','sid',req);raise AssertionError('Duplicate write')
    except ValueError:pass
    store.finish('run',result)
    assert store.begin('run','owner','sid',req)[1] is True
    try:store.begin('run','owner','sid',req.model_copy(update={'resume_id':'other'}));raise AssertionError('Changed payload')
    except ValueError:pass
    store.begin('interrupted','owner','sid',req);store.interrupt('interrupted')
    try:store.begin('interrupted','owner','sid',req);raise AssertionError('Interrupted replay')
    except ValueError:pass
    print('assist_loop_regression_test: OK (dynamic re-analysis, model caps, readback, no repeat, cancellation, owner receipts)')


if __name__=='__main__':asyncio.run(run())
