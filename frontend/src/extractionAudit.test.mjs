// Synthetic/offline only. No real website, candidate, model, or browser operations.
import assert from 'node:assert/strict'
import {createRequire} from 'node:module'
import {readFile} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import vm from 'node:vm'
import {build} from 'vite'
import {createElement} from 'react'
import {renderToStaticMarkup} from 'react-dom/server'

const require=createRequire(import.meta.url)
async function bundle(entry,globals={}){
  const built=await build({configFile:false,logLevel:'error',build:{write:false,minify:false,
    lib:{entry:fileURLToPath(new URL(entry,import.meta.url)),formats:['cjs'],fileName:'extraction-audit-test'},
    rolldownOptions:{external:['react','react/jsx-runtime']},
  }})
  const result=Array.isArray(built)?built[0]:built
  const module={exports:{}}
  vm.runInNewContext(result.output.find(file=>file.type==='chunk').code,{module,exports:module.exports,require,URL,Error,...globals})
  return module.exports
}
const {requestExtractionAudit,extractionQuestionNeedsAttention,extractionAuditSummary,extractionControlLabel,extractionStatusLabel}=await bundle('./extractionAuditReadOnly.ts')
const question={question_id:'q1',title:'是否接受其他城市分配',section_path:['投递意向'],selectors:['[data-fixture="q1"]'],
  control_kind:'radio',required_status:'required',options_status:'group_complete',record_status:'not_applicable',
  observed_options:['是','否'],issues:[],model_review:{question_id:'q1',interpretation:'是否接受其他城市分配',control_kind:'radio',verdict:'clear',issue:'',next_observation:'',evidence:['是否接受其他城市分配']}}
const coverage={version:1,scope:'current_visible_document',capture_status:'observed',observed_controls:2,captured_controls:2,
  intentionally_excluded_controls:0,unmapped_controls:0,question_count:1,verified_questions:1,unclear_questions:0,
  options_pending_questions:0,ambiguous_record_questions:0,embedded_regions:0,unread_shadow_regions:0,pending_sections:[],limitations:[],issues:[]}
const audit={session_id:'read-only-fixture',read_only:true,scope:'current_visible_document',model_status:'complete',model_name:'fixture-model',
  total_questions:1,model_reviewed_questions:1,model_batches:1,observations_requested:0,observations_received:0,images_supplied:0,
  input_manifest:{questions:1,candidate_data_included:false},coverage,limitations:[],questions:[question]}
const calls=[]
const operations={
  async consent(id){calls.push(['consent',id]);return {enabled:false,context_token:'fresh-context'}},
  async audit(id,payload){calls.push(['audit',id,JSON.parse(JSON.stringify(payload))]);return audit},
  async fill(){throw Error('Forbidden filling dependency')},
  async navigate(){throw Error('Forbidden navigation dependency')},
}
assert.equal(await requestExtractionAudit(audit.session_id,{useModel:true,inspectControls:false,includeImages:false},operations),audit)
assert.deepEqual(calls,[['consent',audit.session_id],['audit',audit.session_id,{context_token:'fresh-context',use_model:true,inspect_controls:false,include_images:false}]],'Only refresh context and call the separate read-only endpoint')
calls.length=0
await assert.rejects(()=>requestExtractionAudit(audit.session_id,{useModel:true,inspectControls:true,includeImages:true},operations),/截图授权/)
assert.deepEqual(calls,[['consent',audit.session_id]],'Expired or disabled screenshot consent must stop before model request')
await assert.rejects(()=>requestExtractionAudit(audit.session_id,{useModel:true,inspectControls:false,includeImages:false},
  {...operations,async consent(){return {enabled:true,context_token:''}}}),/版本尚未获取/)
for(const invalid of [{...audit,read_only:false},{...audit,session_id:'other-session'},{...audit,scope:'entire_form'}]){
  await assert.rejects(()=>requestExtractionAudit(audit.session_id,{useModel:false,inspectControls:false,includeImages:false},
    {...operations,async audit(){return invalid}}),/范围不匹配/)
}
let imagePayload
await requestExtractionAudit(audit.session_id,{useModel:true,inspectControls:true,includeImages:true},{
  async consent(){return {enabled:true,context_token:'fresh-authorized'}},
  async audit(_id,payload){imagePayload=JSON.parse(JSON.stringify(payload));return audit},
})
assert.deepEqual(imagePayload,{context_token:'fresh-authorized',use_model:true,inspect_controls:true,include_images:true})
assert.equal(extractionAuditSummary(audit).complete,true)
assert.equal(extractionAuditSummary(audit).reviewed,1)
assert.match(extractionAuditSummary(audit).status,/不代表整张申请表/)
for(const patch of [{model_status:'partial'},{model_status:'unavailable'},{model_status:'not_requested'},
  {model_reviewed_questions:0},{questions:[{...question,model_review:null}]},
  {questions:[{...question,model_review:{...question.model_review,question_id:'unrequested-question'}}]},
  {questions:[{...question,issues:['缺少控件证据']}]},{questions:[{...question,options_status:'observed_subset'}]},
  {questions:[{...question,record_status:'ambiguous'}]},{questions:[{...question,model_review:{...question.model_review,verdict:'conflict'}}]},
  {coverage:{...coverage,capture_status:'partial'}},{coverage:{...coverage,unclear_questions:1}},
  {coverage:{...coverage,options_pending_questions:1}},{coverage:{...coverage,ambiguous_record_questions:1}},
  {coverage:{...coverage,embedded_regions:1}},{coverage:{...coverage,unread_shadow_regions:2}},
  {coverage:{...coverage,pending_sections:['匿名教育经历']}},
  {coverage:{...coverage,unmapped_controls:1}},{total_questions:0,model_reviewed_questions:0,questions:[],coverage:{...coverage,question_count:0}},
])assert.equal(extractionAuditSummary({...audit,...patch}).complete,false,'Do not claim success for missing review or extraction evidence')
assert.equal(extractionAuditSummary({...audit,total_questions:2,model_reviewed_questions:2,questions:[question,question]}).reviewed,1,'Duplicate review ids cannot count twice')
assert.equal(extractionQuestionNeedsAttention(question),false)
assert.equal(extractionQuestionNeedsAttention({...question,model_review:null}),true)
assert.match(extractionStatusLabel('not_marked'),/不是确定选填/)
assert.equal(extractionControlLabel('cascade'),'级联选择')
assert.equal(extractionControlLabel('dropdown'),'下拉选择')

let networkCalls=0
const components=await bundle('./ExtractionAudit.tsx',{window:{location:{protocol:'http:',hostname:'fixture.invalid'},fetch(){networkCalls++;throw Error('SSR cannot request APIs')}}})
const html=renderToStaticMarkup(createElement(components.default||components,{snapshot:{session_id:audit.session_id,url:'https://ats.example.invalid/form',fields:[]},resumeId:'',revision:'',disabled:false,onBusy(){},notify(){}}))
assert.match(html,/只读检查整页识别（不填写）/)
assert.match(html,/不会修改招聘表单/)
assert.match(html,/未展开栏目/)
assert.match(html,/不选择、不填写/)
assert.equal(networkCalls,0)
const partialCoverageHtml=renderToStaticMarkup(createElement(components.ExtractionAuditCoverage,{coverage:{...coverage,
  capture_status:'partial',embedded_regions:2,unread_shadow_regions:3,pending_sections:['匿名教育经历','匿名项目经历']}}))
assert.match(partialCoverageHtml,/未读取嵌入区域 2 个/)
assert.match(partialCoverageHtml,/未读取 Shadow DOM 区域 3 个/)
assert.match(partialCoverageHtml,/未展开或未激活栏目：匿名教育经历/)
assert.match(partialCoverageHtml,/未展开或未激活栏目：匿名项目经历/)
assert.match(partialCoverageHtml,/模型即使审阅完已采集题目，也没有看到所有/)
assert.match(partialCoverageHtml,/本次审计结果，不是上次页面快照/)
const observedCoverageHtml=renderToStaticMarkup(createElement(components.ExtractionAuditCoverage,{coverage}))
assert.match(observedCoverageHtml,/未读取嵌入区域 0 个/)
assert.match(observedCoverageHtml,/不等于跨页/)
const source=await readFile(new URL('./ExtractionAudit.tsx',import.meta.url),'utf8')
assert.doesNotMatch(source,/api\.(assistApplication|runApplicationJourney|planForm|reviewForm|autofillPhase|startBrowser|saveProfile|expandBrowserSection|importResumeWithSite|execute)/,'The audit component has no hidden filling or navigation route')
const app=await readFile(new URL('./App.tsx',import.meta.url),'utf8')
assert.match(app,/\{snapshot&&<ExtractionAudit /)
assert.match(app,/<ExtractionAudit[^\n]*disabled=\{!!busy\|\|stalePage\}/)
assert.doesNotMatch(app.match(/<ExtractionAudit[^\n]*/)?.[0]||'',/canAnalyze/,'Audit remains usable when page-stage classification is uncertain')
const apiSource=await readFile(new URL('./api.ts',import.meta.url),'utf8')
assert.match(apiSource,/extractionAudit:.*extraction-audit/)
console.log('extractionAudit: OK (fresh revision, screenshot gate, truthful full-question coverage, no fill/navigation dependency, isolated UI)')
