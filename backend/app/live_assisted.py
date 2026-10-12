"""Short HTTP start + durable Worker permission + asynchronous local Runner.

This desktop gateway is not a distributed browser service. Its process-local
window binding must be explicitly renewed after restart; receipts survive, but
running writes never automatically resume. No task touches unrelated tabs.
"""
import asyncio
import logging
import os
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query, Request

from .assist_runs import safe_interrupt_reason
from .assisted_runner import AuditedBrowser
from .browser_models import ApplicationAssistProgress, ApplicationAssistRequest
from .backend_v2.assisted_store import AssistedStore
from .backend_v2.repository import Conflict, NotFound
from .backend_v2.messaging import LeaseStore
from .backend_v2.model_budget import MODEL_SCOPE,require_write_control
from .backend_v2.redis_control import ControlUnavailable
from .backend_v2.workbench_storage import database
from .execution_safety import ExecutionTargetChanged
from .storage import current_user_id
from .assisted_reconciliation import reconcile_snapshot

PROCESS_ID=str(uuid4())
TASKS=set()
logger=logging.getLogger(__name__)


def _task_finished(task):
    TASKS.discard(task)
    if task.cancelled():return
    error=task.exception()
    if error is not None:
        # Persistence/lease cleanup can itself fail. Retrieve the exception so
        # asyncio does not print applicant data from an unhandled task error.
        # Do not fabricate a receipt, retry a write or clear its durable lock.
        logger.error('Local Runner ended without clean shutdown (%s); inspect durable receipts',
                     type(error).__name__)


def enabled():
    return os.getenv('ZHIDA_V2_ASSISTED_ENABLED','false').lower()=='true'


def store():
    return AssistedStore(database(),PROCESS_ID)


def create_assisted_router(*, service, require_owner, task_binding, require_local, prepare, operation_lock):
    router=APIRouter()

    @router.post('/api/browser/{session_id}/assist/reconcile')
    async def reconcile(session_id:str,request:Request):
        require_local(request)
        if not enabled():raise HTTPException(409,'持久化辅助填写尚未启用')
        require_owner(session_id)
        records=store();user=current_user_id()
        try:
            # main's browser middleware holds the operation lock. The reader
            # stays on the already bound page; it never searches other tabs.
            pending=records.pending(user)
            snapshot=await service.snapshot(probe_options=False)
            if snapshot.session_id!=session_id:raise ValueError('招聘窗口连接已变化')
            return records.reconcile(user,reconcile_snapshot(snapshot,pending))
        except (Conflict,ValueError,NotFound) as exc:
            raise HTTPException(409,str(exc)) from None

    async def execute(user,identity,session,payload,context):
        records=store()
        lease=None; heartbeat=None; scope_token=None; lease_lost=False
        try:
            # The browser lock is acquired only AFTER Worker authorization.
            # Other read/status requests remain available while the task runs.
            for _ in range(150):
                state=records.state(user,identity)
                if state['cancel_requested']: raise ValueError('你已要求暂停')
                if state['status']=='authorized': break
                if state['status']!='queued': raise ValueError('本轮授权已改变')
                await asyncio.sleep(.2)
            else:
                raise ValueError('队列尚未授权本轮；检查 workflow Worker、Outbox 和 RabbitMQ')
            async with operation_lock():
                require_owner(session)
                if task_binding(session)!=context: raise ValueError('简历、档案或连接发生变化')
                records.claim(user,identity,context[1])
                leases=LeaseStore(records.db)
                lease=leases.acquire(resource_id='desktop:'+user,user_id=user,owner_id=identity,browser=True)
                if not lease:raise ValueError('招聘窗口执行锁未取得；需要核对上一轮执行结果')
                async def renew():
                    nonlocal lease_lost
                    try:
                        while True:
                            await asyncio.sleep(15)
                            if not leases.renew(lease):lease_lost=True;return
                    except asyncio.CancelledError:raise
                    except Exception:lease_lost=True
                heartbeat=asyncio.create_task(renew())
                scope_token=MODEL_SCOPE.set({'user_id':user,'run_id':records.state(user,identity)['run_id']})
                def guard():
                    if lease_lost:raise ValueError('招聘窗口执行锁已过期，停止旧写入')
                    require_write_control(user)
                    require_owner(session)
                    if task_binding(session)!=context: raise ValueError('简历、档案或连接发生变化')
                    records.check(user,identity,context[1])
                runner=AuditedBrowser(service,records,user,identity,guard)
                result=await prepare(session,payload,service_override=runner,
                    on_progress=lambda event:records.emit(user,identity,event),
                    cancelled=lambda:records.state(user,identity)['cancel_requested'])
                records.finish(user,identity,result)
        except asyncio.CancelledError:
            records.interrupt(user,identity,'后端关闭或本轮被中断；先只读核对，不自动重发写入')
            raise
        except BaseException as exc:
            message=(str(exc) if isinstance(exc,ValueError) and '队列尚未授权' in str(exc) else safe_interrupt_reason(exc))
            records.interrupt(user,identity,message+'；已填写内容保留，未最终提交')
        finally:
            if scope_token is not None:MODEL_SCOPE.reset(scope_token)
            if heartbeat:
                heartbeat.cancel()
                await asyncio.gather(heartbeat,return_exceptions=True)
            if lease:
                from sqlalchemy import text
                with records.db.transaction(user) as cx:
                    unknown=bool(cx.execute(text("SELECT 1 FROM zhida.assisted_actions WHERE user_id=:u "
                        "AND round_id=:r AND status IN ('dispatched','unknown') LIMIT 1"),{'u':user,'r':identity}).first())
                LeaseStore(records.db).release(lease,unknown=unknown)

    @router.post('/api/browser/{session_id}/assist/start',status_code=202,response_model=ApplicationAssistProgress)
    async def start(session_id:str,payload:ApplicationAssistRequest,request:Request,
                    run_id:str=Query(max_length=64,pattern=r'^[A-Za-z0-9_-]+$')):
        require_local(request)
        if not enabled(): raise HTTPException(409,'持久化辅助填写尚未启用，未派发写入')
        require_owner(session_id)
        try:require_write_control(current_user_id())
        except (ControlUnavailable,ValueError):
            raise HTTPException(503,'协调服务或执行预算不可用，未派发填写') from None
        context=task_binding(session_id)
        if context[0]!=payload.resume_id or not context[1]:
            raise HTTPException(409,'请先绑定本次简历，再开始辅助填写')
        page=service._require(session_id)
        if hasattr(page,'read_url'): await page.read_url()
        url=service._validate_url(page.url)
        binding={'driver':'safari' if service.context.__class__.__name__=='SafariContext' else 'chromium',
                 'window_id':getattr(service.context,'window_id',None),
                 'tab_index':getattr(page,'tab_index',None)}
        try:
            progress,created=store().start(current_user_id(),run_id,session_id,payload.resume_id,url,
                context[1],payload.model_dump(mode='json'),binding)
        except (Conflict,ValueError,NotFound) as exc:
            raise HTTPException(409,str(exc)) from None
        if created:
            task=asyncio.create_task(execute(current_user_id(),run_id,session_id,payload,context))
            TASKS.add(task);task.add_done_callback(_task_finished)
        return progress

    return router
