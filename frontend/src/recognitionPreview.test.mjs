// Synthetic/offline contract and rendering tests. No browser or model calls.
import assert from 'node:assert/strict'
import {test} from 'node:test'
import {createRequire} from 'node:module'
import {readFile} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import vm from 'node:vm'
import {build} from 'vite'
import {createElement} from 'react'
import {renderToStaticMarkup} from 'react-dom/server'
import {acceptRecognitionPreview,previewScopeKey,previewQuestionClear,recognitionPreviewSummary,samePreviewUrl,validateRecognitionPreview} from './recognitionPreviewContract.ts'

const scope={sessionId:'synthetic-session',resumeId:'synthetic-resume',url:'https://recruit.example/apply?job=1#education'}
const field={id:'q1',question:'除上述选择外，是否还接受其他城市分配',question_status:'verified',control_kind:'radio',
  required:true,required_status:'required',options_status:'group_complete',section_path:['投递意向'],matchable:true,reasons:[],
  options:[{label:'是',disabled:false},{label:'否',disabled:false}],entity_binding:{kind:'person',status:'not_applicable'}}
const coverage={scope:'current_visible_document',status:'observed',observed_controls:2,captured_controls:2,unmapped_controls:0,
  pending_sections:[],embedded_regions:0,unread_shadow_regions:0,limitations:['只代表采集时的当前文档。'],application_complete:false}
const fixture=()=>({schema_version:'live-recognition-preview-v1',session_id:scope.sessionId,capture_id:'capture-fixture',document_id:'document-fixture',
  captured_at:'2026-10-12T08:00:00Z',url:scope.url,title:'合成招聘表单',browser_actions_executed:0,model_calls_executed:0,
  graph_executed:false,persisted:false,preview_only:true,
  recognition:{schema_version:'form-recognition-v1',fields:[structuredClone(field)],issues:[],coverage:structuredClone(coverage),
    needs_observation:false,read_only:true,ready_for_review:false,executable_actions:[]}})

test('accepts exact preview contract without raw values, selectors, or evidence refs',()=>{
  const value=fixture()
  assert.equal(validateRecognitionPreview(value,scope),value)
  assert.deepEqual(recognitionPreviewSummary(value),{total:1,clear:1,attention:0,coverage:'已观察当前文档（不代表整表完整）'})
})

test('rejects action/model/graph/persistence and completeness claims',()=>{
  for(const [key,value] of Object.entries({schema_version:'other',browser_actions_executed:1,model_calls_executed:1,graph_executed:true,persisted:true,preview_only:false})){
    assert.throws(()=>validateRecognitionPreview({...fixture(),[key]:value},scope),/未采纳/)
  }
  for(const patch of [{schema_version:'other'},{read_only:false},{ready_for_review:true},{executable_actions:[{action:'fill'}]}]){
    const value=fixture();Object.assign(value.recognition,patch)
    assert.throws(()=>validateRecognitionPreview(value,scope),/未采纳/)
  }
  const value=fixture();value.recognition.coverage.application_complete=true
  assert.throws(()=>validateRecognitionPreview(value,scope),/未采纳/)
})

test('rejects wrong session, changed URL and incomplete capture identities',()=>{
  for(const patch of [{session_id:'other'},{url:'https://recruit.example/apply?job=2#education'},{url:'https://recruit.example/apply?job=1#work'},
    {capture_id:''},{document_id:''},{captured_at:'invalid'}])assert.throws(()=>validateRecognitionPreview({...fixture(),...patch},scope),/未采纳/)
  assert.equal(samePreviewUrl('https://recruit.example:443/apply','https://recruit.example/apply'),true)
  assert.equal(samePreviewUrl('http://recruit.example:80/apply','http://recruit.example/apply'),true)
  assert.equal(samePreviewUrl('http://recruit.example/apply','https://recruit.example/apply'),false)
  assert.equal(samePreviewUrl('file:///tmp/apply','file:///tmp/apply'),false)
  assert.equal(samePreviewUrl('https://u:p@recruit.example/apply','https://u:p@recruit.example/apply'),false)
})

test('late responses cannot cross session, resume or URL scope',()=>{
  const value=fixture()
  assert.equal(acceptRecognitionPreview(value,scope,{...scope}),value)
  for(const patch of [{sessionId:'other'},{resumeId:'other'},{url:'https://recruit.example/other'}]){
    assert.notEqual(previewScopeKey(scope),previewScopeKey({...scope,...patch}))
    assert.equal(acceptRecognitionPreview(value,scope,{...scope,...patch}),null)
  }
})

test('rejects malformed reports and duplicate or excessive fields',()=>{
  for(const patch of [{fields:null},{fields:[{}]},{fields:[field,field]},{fields:Array(501).fill(field)},{issues:[{}]}]){
    const value=fixture();Object.assign(value.recognition,patch)
    assert.throws(()=>validateRecognitionPreview(value,scope),/未采纳/)
  }
  for(const patch of [{pending_sections:null},{status:'complete'},{captured_controls:-1},{embedded_regions:NaN}]){
    const value=fixture();Object.assign(value.recognition.coverage,patch)
    assert.throws(()=>validateRecognitionPreview(value,scope),/未采纳/)
  }
})

test('unknown question, partial options and ambiguous record never count as clear',()=>{
  for(const patch of [{question:''},{question_status:'unverified'},{matchable:false},{reasons:['group_incomplete']},
    {options_status:'observed_subset'},{options:[]},{control_kind:'unknown'},{entity_binding:{kind:'education',status:'ambiguous'}}]){
    assert.equal(previewQuestionClear({...field,...patch}),false)
  }
  const empty=fixture();empty.recognition.fields=[];empty.recognition.coverage.status='unknown'
  assert.deepEqual(recognitionPreviewSummary(empty),{total:0,clear:0,attention:0,coverage:'读取范围尚未核实'})
})

const require=createRequire(import.meta.url)
const built=await build({configFile:false,logLevel:'error',build:{write:false,minify:false,
  lib:{entry:fileURLToPath(new URL('./RecognitionPreview.tsx',import.meta.url)),formats:['cjs'],fileName:'preview-test'},
  rolldownOptions:{external:['react','react/jsx-runtime']}}})
const output=Array.isArray(built)?built[0]:built,module={exports:{}}
vm.runInNewContext(output.output.find(item=>item.type==='chunk').code,{module,exports:module.exports,require,URL,Date,Error,
  window:{fetch:()=>{throw Error('No network calls allowed during rendering')}}})

test('render shows complete question, control, options, scope gaps and truthful read-only status',()=>{
  const value=fixture()
  value.recognition.fields[0].options[1].disabled=true
  value.recognition.fields[0].current_value='SECRET_CURRENT_VALUE'
  value.recognition.fields[0].entity_binding.anchors=[{value:'SECRET_ANCHOR_VALUE'}]
  value.recognition.coverage.pending_sections=['项目经历'];value.recognition.coverage.status='partial'
  value.recognition.issues=[{code:'pending_sections',field_id:'',detail:'仍有未读取栏目'}]
  const html=renderToStaticMarkup(createElement(module.exports.RecognitionPreviewReport,{result:value}))
  for(const text of [field.question,'单选','是、否（禁用）','项目经历','读取不完整','未运行 LangGraph','未填写、上传或提交','采集时间','仍有未读取栏目'])assert.ok(html.includes(text),text)
  assert.ok(!html.includes('SECRET_CURRENT_VALUE'));assert.ok(!html.includes('SECRET_ANCHOR_VALUE'))
  assert.ok(!html.includes('<input'));assert.ok(!html.includes('<select'))
  const previous=renderToStaticMarkup(createElement(module.exports.RecognitionPreviewReport,{result:value,previous:true}))
  assert.ok(previous.includes('不能作为当前识别结果'))
})

test('issue list maps the exact field ID to question and section, not a duplicate question title',()=>{
  const value=fixture()
  value.recognition.fields=[
    {...structuredClone(field),id:'bachelor-degree',question:'学院名称',section_path:['教育经历','本科']},
    {...structuredClone(field),id:'master-degree',question:'学院名称',section_path:['教育经历','硕士']},
  ]
  value.recognition.issues=[{code:'record_unresolved',field_id:'master-degree',detail:'经历归属尚未核实'}]
  const html=renderToStaticMarkup(createElement(module.exports.RecognitionPreviewReport,{result:value}))
  const list=html.slice(html.indexOf('<ul class="recognition-preview-issues">'),html.indexOf('<div class="recognition-preview-fields">'))
  for(const text of ['2. 学院名称','栏目：教育经历 / 硕士','经历归属尚未核实','master-degree'])assert.ok(list.includes(text),text)
  assert.ok(!list.includes('本科'));assert.ok(!list.includes('bachelor-degree'))
})

test('issue list safely labels global gaps, unknown IDs and missing question text',()=>{
  const value=fixture()
  value.recognition.fields[0].question='';value.recognition.fields[0].section_path=[]
  value.recognition.issues=[
    {code:'pending_sections',field_id:'',detail:'未读取项目栏目'},
    {code:'missing',field_id:'unknown-id',detail:'没有关联字段'},
    {code:'question_missing',field_id:'q1',detail:'缺少题干'},
  ]
  const html=renderToStaticMarkup(createElement(module.exports.RecognitionPreviewReport,{result:value}))
  const list=html.slice(html.indexOf('<ul class="recognition-preview-issues">'),html.indexOf('<div class="recognition-preview-fields">'))
  for(const text of ['页面覆盖范围','未读取项目栏目','题目未关联','unknown-id','题干缺失，不能要求用户猜测','栏目归属未核实','缺少题干'])assert.ok(list.includes(text),text)
})

test('preview wiring has only read-only endpoint, no navigation, fill or model dependency',async()=>{
  const component=await readFile(new URL('./RecognitionPreview.tsx',import.meta.url),'utf8')
  assert.deepEqual([...component.matchAll(/api\.(\w+)\(/g)].map(match=>match[1]),['recognitionPreview'])
  assert.ok(component.includes('scopeRef.current.stale'))
  assert.ok(component.includes('current!==generation.current'))
  assert.ok(component.includes('setReport(null)'))
  const apiSource=await readFile(new URL('./api.ts',import.meta.url),'utf8')
  assert.match(apiSource,/recognitionPreview:.*recognition-preview.*method:'POST'/)
})
