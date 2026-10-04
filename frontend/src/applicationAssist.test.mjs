import assert from 'node:assert/strict'
import {assistanceBackendReady,assistGuard,assistRequest,assistStatus,draftFieldsCompatible,monthPrecisionMemory,needsMonthPrecisionConsent,verifiedDraftEntries} from './applicationAssist.ts'

assert.deepEqual(assistRequest('resume-a'),{resume_id:'resume-a',allow_site_parse:false,use_model:true,max_rounds:3})
assert.equal(assistRequest('resume-a',true).allow_site_parse,true)
assert.equal(assistRequest('resume-a').allow_site_parse,false,'Upload permission must not leak into a later request')
const deferredField={selector:'#id',question_text:'证件号码',semantic_key:'candidate.government_id',container_key:'candidate',field_type:'text',options:[]}
assert.deepEqual(assistRequest('resume-a',false,[deferredField],true),{resume_id:'resume-a',allow_site_parse:false,use_model:true,max_rounds:3,deferred_fields:[deferredField],defer_government_id:true})
assert.equal(assistRequest('resume-a').defer_government_id,undefined,'A per-run ID deferral must not become permanent consent or fact')
assert.equal(assistRequest('resume-a').deferred_fields,undefined,'A later request must not inherit old DOM selectors')
assert.equal(assistanceBackendReady({resume_id:'resume-a'}),false,'A legacy resume-aware server still lacks safe assist')
assert.equal(assistanceBackendReady({}),false)
assert.equal(assistanceBackendReady({resume_id:'resume-a',assistance_version:0}),false)
assert.equal(assistanceBackendReady({assistance_version:1}),false)
assert.equal(assistanceBackendReady({resume_id:'resume-a',assistance_version:'1'}),false)
assert.equal(assistanceBackendReady({resume_id:'resume-a',assistance_version:1}),true)
assert.equal(assistanceBackendReady({resume_id:'',assistance_version:1}),true,'A new server with no bound task may start a task')
assert.equal(assistanceBackendReady({resume_id:'resume-a',assistance_version:2}),true)

const ready={busy:false,backendReady:true,hasSnapshot:true,formReady:true,stalePage:false,hasDraftEdits:false,resumeId:'resume-a',planResumeId:'resume-a'}
assert.equal(assistGuard(ready),'')
for(const [key,value] of Object.entries({busy:true,backendReady:false,hasSnapshot:false,formReady:false,stalePage:true,hasDraftEdits:true,resumeId:'',planResumeId:'resume-b'})){
  assert.notEqual(assistGuard({...ready,[key]:value}),'',`Must guard ${key}`)
}
assert.equal(assistGuard({...ready,planResumeId:undefined}),'','A fresh task may start without a prior plan')
assert.notEqual(assistGuard({...ready,planResumeId:''}),'','A master-only plan is not a plan for the selected resume')

const statuses=['ready_for_review','needs_user','blocked','partial']
assert.equal(new Set(statuses.map(status=>assistStatus(status).title)).size,4)
for(const status of statuses){
  assert.doesNotMatch(assistStatus(status).title,/已投递|投递成功|提交成功/)
}
assert.match(assistStatus('partial').detail,/没有完成全部/)
assert.match(assistStatus('blocked').detail,/不会继续硬填/)
assert.match(assistStatus('ready_for_review').detail,/最终提交由你/)

const site='https://talent.autohome.com.cn/recruit-delivery.html?pid=47900'
for(const marker of ['年月精度','仅有年月','不会擅补1日']){
  assert.equal(needsMonthPrecisionConsent(site,{actions:[{reason:marker}]}),true)
  assert.equal(needsMonthPrecisionConsent(site,{actions:[{reason:'',review_hint:marker}]}),true)
}
assert.equal(needsMonthPrecisionConsent(site,null),false)
assert.equal(needsMonthPrecisionConsent(site,{actions:[{reason:'日期完整'}]}),false)
assert.equal(needsMonthPrecisionConsent('https://other.example/form',{actions:[{reason:'仅有年月'}]}),false)
assert.equal(needsMonthPrecisionConsent('https://talent.autohome.com.cn.evil.example/form',{actions:[{reason:'仅有年月'}]}),false)
assert.equal(needsMonthPrecisionConsent('http://talent.autohome.com.cn/form',{actions:[{reason:'仅有年月'}]}),false)
assert.equal(needsMonthPrecisionConsent('not-a-url',{actions:[{reason:'仅有年月'}]}),false)
assert.equal(monthPrecisionMemory.semantic_key,'application.date_precision')
assert.equal(monthPrecisionMemory.entity_scope,'application')
assert.match(monthPrecisionMemory.value,/不代表真实精确日期.*至今保留/)
const draft={filled:'北京',failed:'上海',unverified:'深圳',skipped:'杭州',missing:'南京'}
const execution={results:[{selector:'filled',status:'filled',verified:true},{selector:'failed',status:'failed',verified:false},{selector:'unverified',status:'filled',verified:false},{selector:'skipped',status:'skipped',verified:true}]}
assert.deepEqual(verifiedDraftEntries(draft,execution),[['filled','北京']],'Only write-and-read-back success can be learned automatically')
assert.deepEqual(verifiedDraftEntries(draft,{results:[...execution.results,{selector:'filled',status:'failed',verified:false}]}),[],'Latest failure must suppress stale success')
const before={session_id:'a',url:site,fields:[{selector:'#city',field_type:'select-one',field_signature:'city',question_text:'期望城市',container_key:'preferred',options:['北京','上海']}]}
assert.equal(draftFieldsCompatible(['#city'],before,structuredClone(before)),true)
assert.equal(draftFieldsCompatible(['#city'],before,{...before,url:'https://other.example/form'}),false)
assert.equal(draftFieldsCompatible([],before,{...before,session_id:'b'}),false)
for(const [key,value] of Object.entries({field_type:'text',field_signature:'other',question_text:'当前城市',container_key:'education',options:['广州'],semantic_key:'other.fact',entity_scope:'other:record',name:'other',label:'其他问题',control_group_key:'other-group',option_label:'其他选项',option_value:'other-value'})){
  assert.equal(draftFieldsCompatible(['#city'],before,{...before,fields:[{...before.fields[0],[key]:value}]}),false,`Retained draft must not silently cross ${key}`)
}
assert.equal(draftFieldsCompatible(['#city'],before,{...before,fields:[]}),false)
console.log('application assist: request defaults, safety guards, statuses, and scoped date consent passed')
