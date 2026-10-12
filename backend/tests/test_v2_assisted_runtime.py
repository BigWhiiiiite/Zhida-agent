"""Real PostgreSQL + LangGraph + typed local Runner, synthetic data only."""
import asyncio
import json
import os
import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.assisted_runner import AuditedBrowser
from app.assisted_reconciliation import reconcile_snapshot
from app.application_graph import validate_plan, create_graph_form_plan, create_graph_model_plan
from app.browser_models import (BrowserSnapshot, PageField, FillAction, FormPlan,
    ApplicationAssistRequest, ApplicationAssistResult, ExecutePlanRequest, PreSubmitCheck,
    ExecutionResult, ActionResult)
from app.models import ResumeProfile
from app.backend_v2.assisted_store import AssistedStore
from app.backend_v2.assisted_workflow import AssistedWorkflowEngine
from app.backend_v2.messaging import LeaseStore
from app.backend_v2.repository import Repository, Conflict
from app.backend_v2.workbench_projection import sync_user
from app.backend_v2.workers import WorkerDispatcher
from test_v2_workflow_worker import event_for, checkpoints
from app.application_assist import prepare_application

URL='https://jobs.synthetic.invalid/resumeEdit?postId=123'


def field(**changes):
    return PageField(selector='#name',label='姓名',question_text='姓名',label_source='explicit',
        field_signature='person:name',section='个人基本信息',control_kind='text',
        observation={'question_status':'verified','options_status':'not_applicable','record_status':'not_applicable'},
        **changes)


def snapshot(*fields):
    return BrowserSnapshot(session_id='s',url=URL,title='合成表单',fields=list(fields or [field()]))


@pytest.fixture
def setup_round(v2_db):
    user='d3-'+uuid4().hex;resume=uuid4().hex
    repo=Repository(v2_db);repo.ensure_user(user)
    with v2_db.transaction(user) as cx:
        cx.execute(text("""INSERT INTO workbench.resumes(id,user_id,filename,label,profile_json,parser,created_at,updated_at)
            VALUES(:id,:u,'sample.txt','合成简历','{}','test','2026-10-12','2026-10-12')"""),{'id':resume,'u':user})
        sync_user(cx,user,{'resumes'})
    store=AssistedStore(v2_db,'process-a');identity=uuid4().hex
    payload={'resume_id':resume,'use_model':False}
    progress,created=store.start(user,identity,'s',resume,URL,'context',payload,{'driver':'synthetic'})
    assert created and progress['status']=='running'
    return user,identity,store,payload


def authorize(fixture):
    user,identity,store,_=fixture;row=store.state(user,identity)
    event=event_for(store.db,user,row['run_id'],1)
    dispatcher=WorkerDispatcher(store.db,engine=AssistedWorkflowEngine(store.db))
    result=dispatcher.dispatch(event_id=event['id'],user_id=user)
    return result,row,dispatcher


def test_short_start_idempotence_and_single_desktop_round(setup_round):
    user,identity,store,payload=setup_round
    receipt,created=store.start(user,identity,'s',payload['resume_id'],URL,'context',payload,{'driver':'synthetic'})
    assert not created and receipt['run_id']==identity
    with pytest.raises(Conflict):
        store.start(user,identity,'s',payload['resume_id'],URL,'context',{**payload,'use_model':True},{'driver':'synthetic'})
    with pytest.raises(Conflict):
        store.start(user,uuid4().hex,'s',payload['resume_id'],URL,'context',payload,{'driver':'synthetic'})


def test_worker_authorizes_once_and_ready_receipt_commits_second_checkpoint(setup_round):
    user,identity,store,_=setup_round
    with pytest.raises(Conflict):store.claim(user,identity,'context')
    result,row,dispatcher=authorize(setup_round)
    assert result['status']=='awaiting_browser' and store.state(user,identity)['status']=='authorized'
    store.claim(user,identity,'context')
    with pytest.raises(Conflict):store.claim(user,identity,'context')
    store.finish(user,identity,ApplicationAssistResult(status='ready_for_review',message='合成终审',
        snapshot=snapshot(),pre_submit=PreSubmitCheck(url=URL,ready=True)))
    run=Repository(store.db).get_run(user,row['run_id'])
    event=event_for(store.db,user,row['run_id'],run['run_revision'])
    assert dispatcher.dispatch(event_id=event['id'],user_id=user)['status']=='ready_for_review'
    states=checkpoints(store.db,user,row['run_id'])
    assert len(states)==2 and states[-1]['state']['trace'][-1]=='audit_coverage'
    assert store.progress(user,identity,'s')['result']['status']=='ready_for_review'


@pytest.mark.parametrize('check',[None,PreSubmitCheck(url=URL,ready=False)])
def test_ready_string_without_submission_check_is_not_ready(setup_round,check):
    user,identity,store,_=setup_round;_,row,dispatcher=authorize(setup_round)
    store.claim(user,identity,'context')
    store.finish(user,identity,ApplicationAssistResult(status='ready_for_review',message='缺少终审',snapshot=snapshot(),pre_submit=check))
    run=Repository(store.db).get_run(user,row['run_id'])
    event=event_for(store.db,user,row['run_id'],run['run_revision'])
    assert dispatcher.dispatch(event_id=event['id'],user_id=user)['status']=='awaiting_browser'


def test_restart_preserves_receipts_and_never_replays_unknown_write(setup_round):
    user,identity,store,_=setup_round;_,row,dispatcher=authorize(setup_round)
    store.claim(user,identity,'context')
    action=store.action_start(user,identity,'set_text',field().model_dump(mode='json'),{'value':'合成姓名'})
    newer=AssistedStore(store.db,'process-b')
    receipt=newer.progress(user,identity,'s')
    assert receipt['status']=='interrupted' and len(newer.pending(user))==1
    with pytest.raises(Conflict):newer.claim(user,identity,'context')
    run=Repository(store.db).get_run(user,row['run_id'])
    event=event_for(store.db,user,row['run_id'],run['run_revision'])
    assert dispatcher.dispatch(event_id=event['id'],user_id=user)['status']=='reconciling'
    observed=snapshot(field(current_value='合成姓名'))
    assert newer.reconcile(user,reconcile_snapshot(observed,newer.pending(user)))=={'resolved':1,'remaining':0,'writes_executed':0}
    assert newer.pending(user)==[]


def test_quarantined_lease_only_clears_after_all_unknowns_read_back(setup_round):
    user,identity,store,_=setup_round;authorize(setup_round);store.claim(user,identity,'context')
    leases=LeaseStore(store.db);lease=leases.acquire(resource_id='desktop:'+user,user_id=user,owner_id=identity,browser=True)
    store.action_start(user,identity,'set_text',field().model_dump(mode='json'),{'value':'合成姓名'})
    store.interrupt(user,identity,'停止')
    assert leases.release(lease,unknown=True)
    assert not leases.acquire(resource_id='desktop:'+user,user_id=user,owner_id='next',browser=True)
    store.reconcile(user,[])
    assert not leases.acquire(resource_id='desktop:'+user,user_id=user,owner_id='next',browser=True)
    # A unique, clearly different current value proves the old operation is
    # not currently satisfied; it does NOT replay that operation.
    receipt=store.reconcile(user,reconcile_snapshot(snapshot(field(current_value='不同')),store.pending(user)))
    assert receipt['remaining']==0
    assert leases.acquire(resource_id='desktop:'+user,user_id=user,owner_id='next',browser=True)


def test_cancel_context_and_tenant_boundaries(setup_round):
    user,identity,store,_=setup_round;authorize(setup_round)
    with pytest.raises(Conflict):store.claim(user,identity,'changed')
    with pytest.raises(LookupError):store.progress(user,identity,'different-session')
    other='d3-other-'+uuid4().hex;Repository(store.db).ensure_user(other)
    with pytest.raises(LookupError):store.state(other,identity)
    store.cancel(user,identity,'s')
    with pytest.raises(Conflict):store.claim(user,identity,'context')


def test_native_run_cancel_also_revokes_local_runner_write_authority(setup_round):
    user,identity,store,_=setup_round;authorize(setup_round)
    row=store.state(user,identity);repo=Repository(store.db);run=repo.get_run(user,row['run_id'])
    repo.cancel_run(user,row['run_id'],expected_revision=run['run_revision'])
    with pytest.raises(Conflict):store.claim(user,identity,'context')
    with pytest.raises(Conflict):store.check(user,identity,'context')
    with pytest.raises(Conflict):store.action_start(user,identity,'set_text',{}, {'value':'x'})


@pytest.mark.parametrize('change',[{'url':URL.replace('123','124')},{'fields':[]},
    {'fields':[field(current_value='合成姓名'),field(current_value='合成姓名')]},
    {'fields':[field(current_value='合成姓名',entity_scope='education:master')]}])
def test_passive_reconciliation_rejects_changed_url_ambiguous_identity(change):
    observed=snapshot(field(current_value='合成姓名')).model_copy(update=change)
    receipt={'id':'a','application_url':URL,'command_type':'set_text','target':field().model_dump(mode='json'),
             'desired':{'value':'合成姓名'}}
    assert reconcile_snapshot(observed,[receipt])==[]


@pytest.mark.parametrize('action',[FillAction(selector='#invented',label='虚构',action='fill',value='x'),
    FillAction(selector='#name',label='姓名',action='ask_user',value='x')])
def test_planning_graph_rejects_model_invented_action(action):
    plan=validate_plan(snapshot(),FormPlan(actions=[action]))
    assert plan.actions[0].action=='skip' and not plan.actions[0].value


def test_actual_local_and_model_graphs_validate_the_same_observed_targets():
    plan=create_graph_form_plan(snapshot(),ResumeProfile(name='合成姓名'))
    assert plan.actions[0].value=='合成姓名'
    async def model(s):return FormPlan(actions=[FillAction(selector='#invented',label='虚构',action='fill',value='x')])
    assert asyncio.run(create_graph_model_plan(snapshot(),model)).actions[0].action=='skip'


@pytest.mark.parametrize('verified,status',[ (True,'filled'),(False,'failed') ])
def test_runner_records_before_browser_write_and_after_final_readback(verified,status):
    calls=[]
    class Store:
        def action_start(self,*args):calls.append('dispatch');return 'a'
        def action_finish(self,*args):calls.append(args[-2])
    class Service:
        async def execute(self,session,request,**kwargs):
            kwargs['audit_action'](request.actions[0],field());calls.append('actual_write')
            return SimpleNamespace(results=[SimpleNamespace(selector='#name',verified=verified,status=status,actual_value='合成姓名')])
    runner=AuditedBrowser(Service(),Store(),'u','r',lambda:None)
    request=ExecutePlanRequest(actions=[FillAction(selector='#name',label='姓名',action='fill',value='合成姓名')])
    asyncio.run(runner.execute('s',request))
    assert calls==['dispatch','actual_write','verified' if verified else 'failed']


def test_interruption_after_dispatch_stays_unknown():
    calls=[]
    class Store:
        def action_start(self,*a):return 'a'
        def action_finish(self,*a):calls.append(a[-2])
    class Service:
        async def execute(self,session,request,**kwargs):
            kwargs['audit_action'](request.actions[0],field());raise ConnectionError('synthetic')
    request=ExecutePlanRequest(actions=[FillAction(selector='#name',label='姓名',action='fill',value='x')])
    with pytest.raises(ConnectionError):asyncio.run(AuditedBrowser(Service(),Store(),'u','r',lambda:None).execute('s',request))
    assert calls==['unknown']


def test_failed_dispatch_persistence_prevents_browser_write():
    calls=[]
    class Store:
        def action_start(self,*a):raise ConnectionError('no durable receipt')
    class Service:
        async def execute(self,session,request,**kwargs):
            kwargs['audit_action'](request.actions[0],field());calls.append('write')
    request=ExecutePlanRequest(actions=[FillAction(selector='#name',label='姓名',action='fill',value='x')])
    with pytest.raises(ConnectionError):asyncio.run(AuditedBrowser(Service(),Store(),'u','r',lambda:None).execute('s',request))
    assert not calls


def test_owned_dropdown_probing_is_bounded_and_empty_reads_are_not_cached():
    probes=[]
    fields=[PageField(selector=f'#select{i}',label='目标城市',question_text='目标城市',label_source='explicit',
        field_type='combobox',control_kind='dropdown') for i in range(8)]
    class Service:
        async def settled_snapshot(self,session):return snapshot(*fields)
        async def snapshot(self,**kw):assert kw['probe_options'] is False;return snapshot(*fields)
        async def observe_form_controls(self,session,selectors,**kw):probes.append(selectors);return []
    runner=AuditedBrowser(Service(),None,'u','r',lambda:None)
    asyncio.run(runner.settled_snapshot('s'));asyncio.run(runner.settled_snapshot('s'))
    assert len(probes)==2 and all(len(p)==4 for p in probes)
    assert not runner.observed


def test_complete_synthetic_workbench_loop_uses_graph_and_durable_runner(setup_round,monkeypatch):
    monkeypatch.setenv('ZHIDA_V2_ASSISTED_ENABLED','true')
    user,identity,store,payload=setup_round;authorize(setup_round);store.claim(user,identity,'context')
    class Service:
        def __init__(self):self.current=snapshot();self.writes=0
        async def settled_snapshot(self,session):return self.current.model_copy(deep=True)
        async def workflow_state(self,session):return SimpleNamespace(navigation_blocker='',stage='profile_form',url=URL)
        async def record_inventory(self,session):return []
        async def pre_submit_check(self,session):return PreSubmitCheck(url=URL,ready=bool(self.current.fields[0].current_value))
        async def execute(self,session,request,**kw):
            rows=[]
            for action in request.actions:
                kw['before_action']();kw['audit_action'](action,self.current.fields[0])
                self.writes+=1;self.current.fields[0].current_value=str(action.value)
                rows.append(ActionResult(selector=action.selector,label=action.label,status='filled',verified=True,actual_value=str(action.value)))
            return ExecutionResult(url=URL,completed=len(rows),skipped=0,failed=0,verified=len(rows),results=rows,
                                   pre_submit=await self.pre_submit_check(session))
    service=Service();runner=AuditedBrowser(service,store,user,identity,lambda:store.check(user,identity,'context'))
    async def no_model(s):raise AssertionError('A confirmed name needs no external model')
    result=asyncio.run(prepare_application(runner,'s',ApplicationAssistRequest(**payload),ResumeProfile(name='合成姓名'),
        guard=lambda:store.check(user,identity,'context'),model_plan=no_model,stamp_plan=lambda p:p,
        on_progress=lambda e:store.emit(user,identity,e)))
    assert result.status=='ready_for_review' and service.writes==1 and result.model_calls==0
    store.finish(user,identity,result)
    with store.db.transaction(user) as cx:
        assert cx.execute(text('SELECT status FROM zhida.assisted_actions WHERE user_id=:u'),{'u':user}).scalar_one()=='verified'
        assert cx.execute(text('SELECT count(*) FROM zhida.model_calls WHERE user_id=:u'),{'u':user}).scalar()==0


def test_new_recognition_gate_rejects_complete_looking_but_unowned_title():
    ambiguous=field().model_copy(update={'label_source':'nearby','observation':None})
    action=FillAction(selector='#name',label='姓名',action='fill',value='合成姓名')
    assert validate_plan(snapshot(ambiguous),FormPlan(actions=[action])).actions[0].action=='skip'


def test_empty_options_are_not_reinterpreted_as_text():
    dropdown=field().model_copy(update={'field_type':'combobox','control_kind':'dropdown','options':[],'observation':None})
    action=FillAction(selector='#name',label='姓名',action='select',value='x')
    assert validate_plan(snapshot(dropdown),FormPlan(actions=[action])).actions[0].action=='skip'


def test_confirmed_memory_projects_exact_scopes_and_revokes_removed_answers(v2_db):
    user='memory-d3-'+uuid4().hex;Repository(v2_db).ensure_user(user)
    memories=[{'id':uuid4().hex,'value':'合成学院','question':'学院','normalized_question':'学院',
        'resume_id':'selected','company_scope':'company','source_host':'jobs.synthetic.invalid',
        'entity_scope':scope,'semantic_key':'education.college'} for scope in ['education:master','education:bachelor']]
    with v2_db.transaction(user) as cx:
        cx.execute(text("INSERT INTO workbench.candidate_profiles(id,user_id,profile_json,created_at,updated_at) "
            "VALUES(:u,:u,:p,'2026-10-12','2026-10-12')"),{'u':user,'p':json.dumps({'application_answer_memory':memories})})
        sync_user(cx,user,{'candidate_profiles'})
        assert cx.execute(text('SELECT count(*) FROM zhida.profile_facts WHERE user_id=:u'),{'u':user}).scalar()==2
        sync_user(cx,user,{'candidate_profiles'})
        assert cx.execute(text('SELECT count(*) FROM zhida.fact_revisions WHERE user_id=:u'),{'u':user}).scalar()==2
        conflict={**memories[0],'id':uuid4().hex,'value':'冲突学院'}
        cx.execute(text('UPDATE workbench.candidate_profiles SET profile_json=:p WHERE user_id=:u'),
            {'u':user,'p':json.dumps({'application_answer_memory':[memories[0],conflict]})})
        sync_user(cx,user,{'candidate_profiles'})
        confirmations=list(cx.execute(text("SELECT r.confirmation FROM zhida.profile_facts f JOIN zhida.fact_revisions r "
            "ON r.id=f.current_revision_id WHERE f.user_id=:u"),{'u':user}).scalars())
        assert set(confirmations)=={'unconfirmed','revoked'}


def test_company_knowledge_projection_indexes_only_mapping_text_and_is_idempotent(v2_db):
    user='knowledge-d3-'+uuid4().hex;Repository(v2_db).ensure_user(user)
    with v2_db.transaction(user) as cx:
        cx.execute(text("INSERT INTO workbench.application_knowledge(id,user_id,site_scope,record_json,embedding_text,updated_at) "
            "VALUES('k',:u,'company-scope','{}','学院对应 education.college','2026-10-12')"),{'u':user})
        sync_user(cx,user,{'application_knowledge'});sync_user(cx,user,{'application_knowledge'})
        assert cx.execute(text('SELECT count(*) FROM rag.source_revisions WHERE user_id=:u'),{'u':user}).scalar()==1
        row=cx.execute(text('SELECT records FROM rag.source_revisions WHERE user_id=:u'),{'u':user}).scalar_one()
        assert row[0]['entity_id']=='k' and '合成学院' not in str(row)
        cx.execute(text('DELETE FROM workbench.application_knowledge WHERE user_id=:u'),{'u':user})
        sync_user(cx,user,{'application_knowledge'})
        assert cx.execute(text('SELECT revoked_at IS NOT NULL FROM rag.knowledge_sources WHERE user_id=:u'),{'u':user}).scalar_one()


def test_production_celery_factory_authorizes_d3_through_real_isolated_broker(setup_round,monkeypatch):
    from celery.contrib.testing.worker import TestWorkController,start_worker
    from kombu import Exchange,Queue
    from app.backend_v2.workers import create_celery_app,confirmed_publish
    from urllib.parse import urlparse,unquote
    broker=os.getenv('ZHIDA_TEST_BROKER_URL','')
    if not broker:pytest.skip('Dedicated synthetic RabbitMQ vhost required')
    assert unquote(urlparse(broker).path).lstrip('/').startswith('zhida_test_')
    for flag in ('WORKFLOW','RECOGNITION','ASSISTED'):monkeypatch.setenv('ZHIDA_V2_'+flag+'_ENABLED','true')
    user,identity,store,_=setup_round;row=store.state(user,identity)
    event=event_for(store.db,user,row['run_id'],1)
    celery=create_celery_app(database_url=store.db.engine.url.render_as_string(hide_password=False),broker_url=broker)
    name='zhida.test.d3.'+uuid4().hex;exchange=Exchange(name,type='direct',durable=True)
    queue=Queue(name,exchange=exchange,routing_key='workflow.advance',alias='workflow.advance',durable=True)
    celery.conf.task_queues=(queue,)
    celery.conf.task_routes={'zhida.v2.workflow.advance':{'queue':name,'routing_key':'workflow.advance'}}
    celery.amqp.queues=celery.amqp.Queues((queue,))
    class BoundedController(TestWorkController):
        def ensure_started(self):
            if not self._on_started.wait(20):
                threading.Thread(target=self.terminate,daemon=True).start()
                raise TimeoutError('Synthetic D3 worker startup exceeded 20 seconds')
    try:
        with start_worker(celery,perform_ping_check=False,pool='solo',concurrency=1,queues=[name],
                          loglevel='ERROR',WorkController=BoundedController):
            confirmed_publish(celery,event);confirmed_publish(celery,event)
            deadline=time.monotonic()+20
            while store.state(user,identity)['status']!='authorized' and time.monotonic()<deadline:time.sleep(.1)
            assert store.state(user,identity)['status']=='authorized'
            assert checkpoints(store.db,user,row['run_id'])[0]['state']['reason']=='local_round_authorized'
    finally:
        with celery.connection_for_write() as connection:
            queue(connection).delete(if_unused=False,if_empty=False)
            exchange(connection).delete(if_unused=False)
        celery.close()


@pytest.mark.parametrize('degree_verified',[True,False])
def test_hidden_education_fields_are_read_only_after_verified_degree(monkeypatch,degree_verified):
    monkeypatch.setenv('ZHIDA_V2_ASSISTED_ENABLED','true')
    def edu(key,label,kind='text',value=''):
        return PageField(selector='#'+key,label=label,question_text=label,label_source='explicit',field_type=kind,
            semantic_key='education.'+key,container_key='education:synthetic-empty',record_evidence='phoenix-owned',
            section='教育经历',current_value=value,field_signature='education:'+key,
            options=['本科','硕士'] if key=='degree' else [],options_capture='native_complete' if key=='degree' else '')
    class Service:
        def __init__(self):self.current=snapshot(edu('degree','学历','select-one'));self.writes=[]
        async def settled_snapshot(self,session):return self.current.model_copy(deep=True)
        async def workflow_state(self,session):return SimpleNamespace(navigation_blocker='',stage='profile_form',url=URL)
        async def record_inventory(self,session):return [{'kind':'education','record_keys':['education:synthetic-empty'],
            'record_count':1,'label':'教育经历','selector':''}]
        async def pre_submit_check(self,session):return PreSubmitCheck(url=URL,ready=False)
        async def execute(self,session,request,**kwargs):
            rows=[]
            for a in request.actions:
                self.writes.append((a.selector,a.value));f=next(f for f in self.current.fields if f.selector==a.selector)
                if a.selector=='#degree' and degree_verified:
                    f.current_value=str(a.value);self.current.fields.extend([edu('school','学校'),edu('college','学院')])
                elif degree_verified:f.current_value=str(a.value)
                rows.append(ActionResult(selector=a.selector,label=a.label,status='filled' if degree_verified else 'failed',verified=degree_verified))
            return ExecutionResult(url=URL,completed=len(rows) if degree_verified else 0,skipped=0,failed=0 if degree_verified else len(rows),
                verified=len(rows) if degree_verified else 0,results=rows,
                pre_submit=await self.pre_submit_check(session))
    service=Service()
    async def no_model(s):raise AssertionError('No model for explicitly assigned empty education slot')
    result=asyncio.run(prepare_application(service,'s',ApplicationAssistRequest(resume_id='synthetic',use_model=False),
        ResumeProfile(education=[{'school':'合成硕士学校','degree':'硕士','college':'合成硕士学院'}]),
        guard=lambda:None,model_plan=no_model,stamp_plan=lambda p:p))
    assert service.writes[0]==('#degree','硕士')
    if degree_verified:
        assert ('#school','合成硕士学校') in service.writes
        assert ('#college','合成硕士学院') in service.writes
    else:
        assert service.writes==[('#degree','硕士')]
        assert result.status=='blocked'
