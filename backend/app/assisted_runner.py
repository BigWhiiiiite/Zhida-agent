"""Local typed browser Runner using the existing guarded driver, not raw CDP.

Only methods explicitly called by the bounded preparation loop can write.
Each real value write gets a durable dispatch BEFORE touching the widget and
an outcome AFTER final settling/readback. Cancellation never means rollback.
"""
from .browser_models import ExecutePlanRequest
from .form_field_policy import is_declaration
from .form_observation import extraction_block_reason, merge_observed_metadata
from .backend_v2.repository import digest


def command_kind(action, field):
    if action.action=='check': return 'check_option'
    if field.control_kind=='calendar' or field.field_type in {'date','month','datetime-local'}: return 'set_date'
    if action.action=='select': return 'select_option'
    return 'set_text'


class AuditedBrowser:
    def __init__(self, service, store, user, round_id, guard):
        self.service,self.store,self.user,self.round_id,self.guard=service,store,user,round_id,guard
        self.observed = set()
        self.metadata = []

    def __getattr__(self, name):
        return getattr(self.service,name)

    async def settled_snapshot(self, session):
        self.guard()
        snapshot = await self.service.settled_snapshot(session)
        # A bounded, owned-control observation opens/closes menus, but does not
        # choose values. Do not sweep unknown widgets or declarations.
        candidates=[]
        for field in snapshot.fields:
            identity=digest([field.selector,field.field_signature,field.question_text,field.container_key,
                             field.control_kind,field.current_value,field.semantic_key,field.record_evidence])
            if (field.field_type=='combobox' and not field.options and identity not in self.observed
                    and not is_declaration(field) and not extraction_block_reason(field)):
                candidates.append((field,identity))
        if candidates:
            selected=candidates[:4]
            self.guard()
            observations=await self.service.observe_form_controls(session,[f.selector for f,_ in selected],expected_snapshot=snapshot)
            self.guard()
            refreshed=await self.service.snapshot(probe_options=False)
            if refreshed.url!=snapshot.url:
                raise ValueError('补读选项时网页已离开本次申请表；旧证据作废')
            self.metadata.extend(observations)
            # Only successfully returned observations are cached; timeouts and
            # changed fields are still inspected afresh after a dependency.
            returned={item.selector for item in observations if item.options or item.cascade_observation}
            self.observed.update(identity for f,identity in selected if f.selector in returned)
            snapshot=refreshed
        current={f.selector:f for f in snapshot.fields}
        identity_keys=('question_text','field_type','control_kind','container_key','control_group_key',
                       'entity_scope','section','current_value','required','semantic_key','field_signature','record_evidence')
        self.metadata=[sample for sample in self.metadata if sample.selector in current and all(
            getattr(sample,k)==getattr(current[sample.selector],k) for k in identity_keys)]
        merge_observed_metadata(snapshot,self.metadata)
        return snapshot

    async def execute(self, session, request, *args, **kwargs):
        self.guard(); pending={}
        def dispatched(action,field):
            self.guard()
            # The underlying driver already checks live Document/field/group.
            # Store the exact identity used at that write, never model-created
            # selector identity or a screen-coordinate guess.
            target=field.model_dump(mode='json')
            target.pop('current_value',None)
            identity=self.store.action_start(self.user,self.round_id,command_kind(action,field),
                target,{'value':action.value,'field_identity':digest(target)})
            pending[action.selector]=identity
        try:
            result=await self.service.execute(session,request,*args,**kwargs,audit_action=dispatched)
            by_selector={r.selector:r for r in result.results}
            for selector,identity in pending.items():
                item=by_selector.get(selector)
                status='verified' if item and item.verified and item.status=='filled' else 'failed'
                self.store.action_finish(self.user,identity,status,{'readback_verified':status=='verified',
                    'actual_hash':digest(item.actual_value) if item else None})
            return result
        except BaseException:
            for identity in pending.values():
                try:
                    self.store.action_finish(self.user,identity,'unknown',{'reason':'round_interrupted'})
                except Exception:
                    pass  # An earlier final readback may already have committed.
            raise

    async def expand_missing_section(self, session, candidate):
        self.guard()
        identity=self.store.action_start(self.user,self.round_id,'add_record',
            {'selector':candidate['selector'],'kind':candidate['kind']},
            {'before_count':candidate['record_count']})
        try:
            result=await self.service.expand_missing_section(session,candidate)
            self.guard()
            inventory=await self.service.record_inventory(session)
            after=next((i for i in inventory if i['kind']==candidate['kind']),None)
            verified=bool(after and after['record_count']==candidate['record_count']+1)
            self.store.action_finish(self.user,identity,'verified' if verified else 'failed',
                {'record_count':after['record_count'] if after else None})
            if not verified: raise ValueError('新增记录未通过数量回读，不会重复点击')
            return result
        except BaseException:
            try:self.store.action_finish(self.user,identity,'unknown',{'reason':'record_outcome_unknown'})
            except Exception:pass
            raise

    async def import_resume_with_site_parser(self, session, path, allowed):
        if not allowed: raise ValueError('附件上传未得到本轮授权')
        self.guard()
        identity=self.store.action_start(self.user,self.round_id,'upload_file',
            {'attachment':'selected_resume'}, {'file_reference':'selected_resume_original'})
        try:
            result=await self.service.import_resume_with_site_parser(session,path,allowed)
            self.guard()
            self.store.action_finish(self.user,identity,'verified' if result.status!='needs_user_action' else 'failed',
                {'site_parse_status':result.status})
            return result
        except BaseException:
            self.store.action_finish(self.user,identity,'unknown',{'reason':'upload_outcome_unknown'})
            raise
