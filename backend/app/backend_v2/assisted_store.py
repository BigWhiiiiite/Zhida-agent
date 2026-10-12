"""Durable D3 workbench bridge with server-owned browser/resume references.

Neither caller-provided DOM nor browser cookies enter the graph queue. Desktop
authorization is established by main's existing owner/window binding, not JSON.
"""
from uuid import uuid4
from sqlalchemy import text

from .assisted_workflow import D3_VERSION
from .messaging import enqueue_job
from .repository import Conflict, NotFound, digest, emit_event, encode, require_row


class AssistedStore:
    def __init__(self, db, process_id):
        self.db,self.process_id = db,process_id

    def _round(self, cx, user, identity, session=None, *, lock=False):
        if lock:
            # Same order as Worker checkpoint commit: run, then local round.
            run = cx.execute(text('SELECT run_id FROM zhida.assisted_rounds WHERE user_id=:u AND id=:id'),
                {'u':user,'id':identity}).scalar()
            if run:
                cx.execute(text('SELECT id FROM zhida.application_runs WHERE user_id=:u AND id=:r FOR UPDATE'),
                    {'u':user,'r':run}).first()
        try:
            row = require_row(cx,'SELECT * FROM zhida.assisted_rounds WHERE user_id=:u AND id=:id'+
                (' FOR UPDATE' if lock else ''),{'u':user,'id':identity})
        except NotFound:
            raise LookupError('本轮记录不存在或不属于你') from None
        if session is not None and row['legacy_session_id']!=session:
            raise LookupError('本轮记录不属于这个招聘窗口')
        return row

    def start(self,user,identity,session,resume_id,url,context_hash,payload,binding):
        fingerprint = digest(payload)
        with self.db.transaction(user) as cx:
            require_row(cx,'SELECT id FROM zhida.users WHERE id=:u FOR UPDATE',{'u':user})
            old = cx.execute(text('SELECT * FROM zhida.assisted_rounds WHERE user_id=:u AND id=:id'),
                {'u':user,'id':identity}).mappings().first()
            if old:
                if (old['legacy_session_id'],old['legacy_resume_id'],old['request_hash'])!=(session,resume_id,fingerprint):
                    raise Conflict('同一填写轮次不能更换招聘窗口、简历或授权')
                return old['progress'],False
            active = cx.execute(text("SELECT * FROM zhida.assisted_rounds WHERE user_id=:u "
                "AND status IN ('queued','authorized','running')"),{'u':user}).mappings().first()
            if active and active['process_id']!=self.process_id:
                stale=self._round(cx,user,active['id'],lock=True)
                self._interrupt(cx,user,stale,'后端已重启，本轮已停止；未自动重发填写')
                active=None
            if active:
                raise Conflict('已有辅助填写在运行，请等待或暂停；不会同时写入两个招聘窗口')
            if cx.execute(text("SELECT 1 FROM zhida.assisted_actions WHERE user_id=:u "
                "AND status IN ('dispatched','unknown') LIMIT 1"),{'u':user}).first():
                raise Conflict('上一轮存在结果未明的写入，请先使用“只读核对未明操作”；不会重发')
            mapped = require_row(cx,"SELECT core_id,payload FROM workbench.projections "
                "WHERE user_id=:u AND kind='resume' AND legacy_id=:id AND deleted_at IS NULL",
                {'u':user,'id':resume_id})
            revision = mapped['payload']['revision_id']
            active_run = cx.execute(text("SELECT * FROM zhida.application_runs WHERE user_id=:u AND status IN "
                "('queued','running','awaiting_user','awaiting_browser','retry_wait','reconciling') FOR UPDATE"),
                {'u':user}).mappings().first()
            if active_run:
                previous_round = cx.execute(text('SELECT status FROM zhida.assisted_rounds WHERE user_id=:u AND run_id=:r '
                    'ORDER BY created_at DESC LIMIT 1'),{'u':user,'r':active_run['id']}).scalar()
                if active_run['workflow_version']!=D3_VERSION or previous_round not in {'finished','interrupted'}:
                    raise Conflict('已有其他原生任务，请先完成或取消')
                cx.execute(text("UPDATE zhida.application_runs SET status='cancelled',run_revision=run_revision+1 "
                    "WHERE user_id=:u AND id=:r"),{'u':user,'r':active_run['id']})
                emit_event(cx,user,active_run['id'],'run.superseded',{'reason':'explicit_new_round'})
            device = 'desktop:'+user
            browser = str(uuid4()); run = str(uuid4())
            cx.execute(text("INSERT INTO zhida.devices(id,user_id,name) VALUES(:id,:u,'Local workbench Runner') "
                "ON CONFLICT(id) DO NOTHING"),{'id':device,'u':user})
            cx.execute(text("""INSERT INTO zhida.browser_sessions(id,user_id,device_id,driver,binding)
                VALUES(:id,:u,:d,:driver,CAST(:binding AS jsonb))"""),{'id':browser,'u':user,'d':device,
                'driver':binding['driver'],'binding':encode({**binding,'legacy_session_id':session})})
            cx.execute(text("""INSERT INTO zhida.application_runs(id,user_id,resume_revision_id,browser_session_id,
                application_url,idempotency_key,request_hash,workflow_version,budget)
                VALUES(:r,:u,:rev,:b,:url,:key,:hash,:v,CAST(:budget AS jsonb))"""),
                {'r':run,'u':user,'rev':revision,'b':browser,'url':url,'key':'assist:'+identity,
                 'hash':fingerprint,'v':D3_VERSION,'budget':encode({'max_advances':8,'max_graph_steps':64})})
            progress = {'run_id':identity,'status':'running','phase':'queued','message':'辅助填写已排队；尚未开始写入',
                        'events':[],'result':None,'cancel_requested':False}
            cx.execute(text("""INSERT INTO zhida.assisted_rounds(id,user_id,run_id,legacy_session_id,legacy_resume_id,
                request_hash,context_hash,process_id,request,progress)
                VALUES(:id,:u,:r,:s,:resume,:hash,:context,:process,CAST(:request AS jsonb),CAST(:p AS jsonb))"""),
                {'id':identity,'u':user,'r':run,'s':session,'resume':resume_id,'hash':fingerprint,
                 'context':context_hash,'process':self.process_id,'request':encode(payload),'p':encode(progress)})
            emit_event(cx,user,run,'run.created',{'workflow':D3_VERSION,'local_round_id':identity})
            enqueue_job(cx,user_id=user,kind='workflow.advance',aggregate_id=run,expected_revision=1,
                idempotency_key='assist-authorize:'+identity)
            return progress,True

    def state(self,user,identity):
        with self.db.transaction(user) as cx:
            return self._round(cx,user,identity)

    def progress(self,user,identity,session):
        with self.db.transaction(user) as cx:
            row = self._round(cx,user,identity,session,lock=True)
            if row['process_id']!=self.process_id and row['status'] in {'queued','authorized','running'}:
                self._interrupt(cx,user,row,'后端已重启，本轮已停止；先只读核对网页，不会自动重发填写')
                row = self._round(cx,user,identity,session)
            return row['progress']

    def claim(self,user,identity,context_hash):
        with self.db.transaction(user) as cx:
            row = self._round(cx,user,identity,lock=True)
            self._assert_live_run(cx,user,row)
            if row['process_id']!=self.process_id or row['context_hash']!=context_hash or row['cancel_requested']:
                raise Conflict('任务上下文或本机进程已变化，旧授权不能继续')
            if row['status']!='authorized':
                raise Conflict('尚未取得 Worker 授权，或本轮已被执行')
            cx.execute(text("UPDATE zhida.assisted_rounds SET status='running',updated_at=now() "
                "WHERE user_id=:u AND id=:id"),{'u':user,'id':identity})

    def check(self,user,identity,context_hash):
        with self.db.transaction(user) as cx:
            row=self._round(cx,user,identity)
            self._assert_live_run(cx,user,row)
            if (row['status']!='running' or row['cancel_requested'] or row['process_id']!=self.process_id
                    or row['context_hash']!=context_hash):
                raise ValueError('你已要求暂停，或本轮资料已变化；停止后续写入')

    def _assert_live_run(self,cx,user,row):
        status=require_row(cx,'SELECT status FROM zhida.application_runs WHERE user_id=:u AND id=:r',
            {'u':user,'r':row['run_id']})['status']
        if status in {'cancelled','failed','ready_for_review'}:
            raise Conflict('原生任务已取消或结束，旧执行权不能继续填写')

    def emit(self,user,identity,event):
        with self.db.transaction(user) as cx:
            row = self._round(cx,user,identity,lock=True)
            p = row['progress']
            p['phase'],p['message'] = event.kind,event.message
            p['events'] = [*p.get('events',[]),event.model_dump(mode='json')][-500:]
            p['cancel_requested']=row['cancel_requested']
            cx.execute(text('UPDATE zhida.assisted_rounds SET progress=CAST(:p AS jsonb),updated_at=now() '
                'WHERE user_id=:u AND id=:id'),{'u':user,'id':identity,'p':encode(p)})

    def _wake(self,cx,user,run):
        row = require_row(cx,'SELECT run_revision,status FROM zhida.application_runs WHERE user_id=:u AND id=:r FOR UPDATE',
            {'u':user,'r':run})
        if row['status'] in {'cancelled','failed','ready_for_review'}: return
        rev = row['run_revision']+1
        cx.execute(text("UPDATE zhida.application_runs SET run_revision=:rev,status='queued' WHERE user_id=:u AND id=:r"),
            {'u':user,'r':run,'rev':rev})
        enqueue_job(cx,user_id=user,kind='workflow.advance',aggregate_id=run,expected_revision=rev,
            idempotency_key=f'assist-result:{run}:{rev}')

    def finish(self,user,identity,result):
        with self.db.transaction(user) as cx:
            row = self._round(cx,user,identity,lock=True)
            self._assert_live_run(cx,user,row)
            if row['status']!='running': raise Conflict('本轮执行状态已变化')
            if row['cancel_requested']:
                result=result.model_copy(update={'status':'partial','message':'已按要求暂停；已填写内容保留，请核对官网，尚未提交'})
            p = row['progress']; body=result.model_dump(mode='json')
            p.update(status='finished',phase=result.status,message=result.message,result=body)
            cx.execute(text("UPDATE zhida.assisted_rounds SET status='finished',progress=CAST(:p AS jsonb),"
                "result=CAST(:result AS jsonb),updated_at=now() WHERE user_id=:u AND id=:id"),
                {'u':user,'id':identity,'p':encode(p),'result':encode(body)})
            self._wake(cx,user,row['run_id'])

    def _interrupt(self,cx,user,row,message):
        p = row['progress']; p.update(status='interrupted',phase='interrupted',message=message)
        cx.execute(text("UPDATE zhida.assisted_rounds SET status='interrupted',progress=CAST(:p AS jsonb),updated_at=now() "
            "WHERE user_id=:u AND id=:id"),{'u':user,'id':row['id'],'p':encode(p)})
        cx.execute(text("UPDATE zhida.assisted_actions SET status='unknown' WHERE user_id=:u AND round_id=:id "
            "AND status='dispatched'"),{'u':user,'id':row['id']})
        self._wake(cx,user,row['run_id'])

    def interrupt(self,user,identity,message):
        with self.db.transaction(user) as cx:
            row = self._round(cx,user,identity,lock=True)
            if row['status'] in {'finished','interrupted'}: return
            self._interrupt(cx,user,row,message)

    def cancel(self,user,identity,session):
        with self.db.transaction(user) as cx:
            row = self._round(cx,user,identity,session,lock=True)
            cx.execute(text('UPDATE zhida.assisted_rounds SET cancel_requested=true WHERE user_id=:u AND id=:id'),
                {'u':user,'id':identity})
            p=row['progress'];p['cancel_requested']=True
            cx.execute(text('UPDATE zhida.assisted_rounds SET progress=CAST(:p AS jsonb) WHERE user_id=:u AND id=:id'),
                {'u':user,'id':identity,'p':encode(p)})
            return p

    def action_start(self,user,identity,kind,target,desired):
        action = str(uuid4())
        with self.db.transaction(user) as cx:
            row=self._round(cx,user,identity,lock=True)
            self._assert_live_run(cx,user,row)
            if row['status']!='running' or row['cancel_requested']: raise Conflict('本轮已暂停')
            if cx.execute(text("SELECT 1 FROM zhida.assisted_actions WHERE user_id=:u AND round_id=:r "
                "AND status='unknown' LIMIT 1"),{'u':user,'r':identity}).first():
                raise Conflict('上一项操作结果未明，不能继续写入')
            cx.execute(text("""INSERT INTO zhida.assisted_actions(id,user_id,round_id,command_type,command_hash,target,desired,status)
                VALUES(:id,:u,:r,:kind,:hash,CAST(:target AS jsonb),CAST(:desired AS jsonb),'dispatched')"""),
                {'id':action,'u':user,'r':identity,'kind':kind,'hash':digest([kind,target,desired]),
                 'target':encode(target),'desired':encode(desired)})
            return action

    def action_finish(self,user,action,status,evidence):
        if status not in {'verified','failed','unknown'}: raise ValueError('Invalid receipt')
        with self.db.transaction(user) as cx:
            row=require_row(cx,'SELECT * FROM zhida.assisted_actions WHERE user_id=:u AND id=:id FOR UPDATE',
                {'u':user,'id':action})
            if row['status'] not in {'dispatched','unknown'}: raise Conflict('操作已有终态回执')
            cx.execute(text('UPDATE zhida.assisted_actions SET status=:status,evidence=CAST(:e AS jsonb),updated_at=now() '
                'WHERE user_id=:u AND id=:id'),{'u':user,'id':action,'status':status,'e':encode(evidence)})

    def pending(self, user):
        """Read receipts only; a live round cannot be reconciled underneath it."""
        with self.db.transaction(user) as cx:
            active = cx.execute(text("SELECT id FROM zhida.assisted_rounds WHERE user_id=:u "
                "AND status IN ('queued','authorized','running') AND process_id=:p"),
                {'u':user,'p':self.process_id}).first()
            if active: raise Conflict('本轮仍在运行，请先暂停并等待本轮结束')
            return [dict(row) for row in cx.execute(text("""SELECT a.*,r.application_url
                FROM zhida.assisted_actions a JOIN zhida.assisted_rounds s
                  ON s.id=a.round_id AND s.user_id=a.user_id
                JOIN zhida.application_runs r ON r.id=s.run_id AND r.user_id=s.user_id
                WHERE a.user_id=:u AND a.status IN ('dispatched','unknown')"""),{'u':user}).mappings()]

    def reconcile(self, user, outcomes):
        """Commit passive evidence, then clear quarantine only if nothing is unknown.

        No value is written and no old command is replayed. Unreadable/ambiguous
        controls remain unknown. Lock the desktop lease to fence other runners.
        """
        with self.db.transaction(user) as cx:
            require_row(cx,'SELECT id FROM zhida.users WHERE id=:u FOR UPDATE',{'u':user})
            lease=cx.execute(text('SELECT * FROM zhida.runner_leases WHERE resource_id=:r AND user_id=:u FOR UPDATE'),
                {'u':user,'r':'desktop:'+user}).mappings().first()
            if lease and cx.execute(text('SELECT :expiry>now()'),{'expiry':lease['expires_at']}).scalar():
                raise Conflict('招聘窗口仍被执行器占用，请稍后只读核对')
            active=cx.execute(text("SELECT id FROM zhida.assisted_rounds WHERE user_id=:u "
                "AND status IN ('queued','authorized','running') AND process_id=:p"),
                {'u':user,'p':self.process_id}).first()
            if active:raise Conflict('本轮仍在执行，未修改操作记录')
            changed=0
            for action_id,status,evidence in outcomes:
                if status not in {'verified','failed'}:continue
                changed+=cx.execute(text("UPDATE zhida.assisted_actions SET status=:s,evidence=CAST(:e AS jsonb),updated_at=now() "
                    "WHERE user_id=:u AND id=:id AND status IN ('dispatched','unknown')"),
                    {'u':user,'id':action_id,'s':status,'e':encode(evidence)}).rowcount
            remaining=cx.execute(text("SELECT count(*) FROM zhida.assisted_actions WHERE user_id=:u "
                "AND status IN ('dispatched','unknown')"),{'u':user}).scalar_one()
            if not remaining:
                cx.execute(text("UPDATE zhida.runner_leases SET needs_reconciliation=false WHERE user_id=:u AND resource_id=:r"),
                    {'u':user,'r':'desktop:'+user})
            return {'resolved':changed,'remaining':remaining,'writes_executed':0}
