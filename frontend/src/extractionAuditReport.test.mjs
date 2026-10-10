// Synthetic/offline only. Exporting must not contact an API, model or website.
import assert from 'node:assert/strict'
import {createRequire} from 'node:module'
import {readFile} from 'node:fs/promises'
import {fileURLToPath} from 'node:url'
import vm from 'node:vm'
import {build} from 'vite'
import {createElement} from 'react'
import {renderToStaticMarkup} from 'react-dom/server'

const require=createRequire(import.meta.url)
async function bundle(entry){
  const built=await build({configFile:false,logLevel:'error',build:{write:false,minify:false,
    lib:{entry:fileURLToPath(new URL(entry,import.meta.url)),formats:['cjs'],fileName:'audit-report-test'},
    rolldownOptions:{external:['react','react/jsx-runtime']},
  }})
  const result=Array.isArray(built)?built[0]:built
  const module={exports:{}}
  vm.runInNewContext(result.output.find(file=>file.type==='chunk').code,{
    module,exports:module.exports,require,URL,Blob,Error,
    window:{location:{protocol:'http:',hostname:'fixture.invalid'},fetch(){throw Error('Report export must never request an API')}},
    fetch(){throw Error('Report export must never request an API')},
  })
  return module.exports
}
const {createExtractionAuditReport,serializeExtractionAuditReport,canExportExtractionAuditReport,
  downloadExtractionAuditReport,copyExtractionAuditReport}=await bundle('./extractionAuditReport.ts')
const coverage={version:1,scope:'current_visible_document',capture_status:'partial',observed_controls:5,captured_controls:3,
  intentionally_excluded_controls:1,unmapped_controls:1,question_count:1,verified_questions:0,unclear_questions:1,
  options_pending_questions:1,ambiguous_record_questions:1,embedded_regions:2,unread_shadow_regions:3,
  pending_sections:['匿名教育经历'],limitations:['嵌入区域未读取'],issues:[{label:'匿名日期',reason:'日历尚未观察',selector:'#fixture-date'}],
  current_value:'forbidden-coverage-value',raw_html:'forbidden-html'}
const review={question_id:'q1',interpretation:'选择匿名日期',control_kind:'date',verdict:'needs_observation',
  issue:'精度待确认',next_observation:'观察日历',evidence:['匿名日期'],answer:'forbidden-model-answer'}
const question={question_id:'q1',title:'匿名日期',section_path:['匿名经历'],selectors:['#fixture-date'],control_kind:'date',
  required_status:'not_marked',options_status:'calendar',record_status:'ambiguous',observed_options:[],issues:['日期精度待核实'],model_review:review,
  current_value:'forbidden-field-value',image_data_url:'data:image/png;base64,forbidden-image'}
const audit={session_id:'forbidden-session-identifier',read_only:true,scope:'current_visible_document',model_status:'partial',model_name:'fixture-model',
  total_questions:1,model_reviewed_questions:1,model_batches:1,observations_requested:2,observations_received:1,images_supplied:1,
  input_manifest:{captured_fields:5,questions_sent:1,credential_controls_excluded:1,candidate_profile_sent:false,current_values_sent:false,
    all_collected_questions_included:true,hidden_or_future_questions_covered:false,unread_embedded_regions:2,unread_shadow_regions:3,
    pending_sections:['匿名教育经历'],surface_inventory_status:'partial',noninteractive_only_selectors:['#fixture-upload'],
    raw_account:'forbidden-account',current_values:['forbidden-input'],images:['data:image/png;base64,forbidden-image']},
  coverage,limitations:['还有未读取内容'],questions:[question],candidate_profile:{name:'forbidden-profile'}}
const original=JSON.stringify(audit)
const serialized=serializeExtractionAuditReport(audit)
const report=JSON.parse(serialized)
assert.equal(report.report_version,1)
assert.equal(report.model_status,'partial','An export is evidence, not a claim of successful recognition')
assert.equal(report.images_supplied,1,'Keep only the image count, not the image bytes')
assert.equal(report.coverage.embedded_regions,2)
assert.equal(report.coverage.unread_shadow_regions,3)
assert.deepEqual(report.coverage.pending_sections,['匿名教育经历'])
assert.deepEqual(report.coverage.issues,[{label:'匿名日期',reason:'日历尚未观察',selector:'#fixture-date'}])
assert.equal(report.input_manifest.current_values_sent,false)
assert.equal(report.input_manifest.all_collected_questions_included,true)
assert.equal(report.input_manifest.hidden_or_future_questions_covered,false)
assert.equal(report.input_manifest.unread_embedded_regions,2)
assert.equal(report.input_manifest.unread_shadow_regions,3)
assert.equal(report.questions[0].model_review.evidence[0],'匿名日期')
assert.equal(report.questions[0].required_status,'not_marked')
assert.equal(report.questions[0].record_status,'ambiguous')
assert.doesNotMatch(serialized,/forbidden-|image_data_url|current_value"|raw_html|candidate_profile"|raw_account/)
assert.equal(JSON.stringify(audit),original,'Export must not change cached evidence')
const withoutReview=createExtractionAuditReport({...audit,questions:[{...question,model_review:null}]})
assert.equal(withoutReview.questions[0].model_review,null,'Unreviewed questions remain explicitly unreviewed')
const account='fixture.person@example.invalid',phone='13800138000',identity='110101200001011234',image='data:image/png;base64,'+'A'.repeat(120)
const privacy=serializeExtractionAuditReport({...audit,limitations:[account,phone,identity,image],
  questions:[{...question,selectors:[`[data-fixture="${account}"]`],title:account,observed_options:[phone]}]})
for(const privateText of [account,phone,identity,image])assert.equal(privacy.includes(privateText),false)
assert.match(privacy,/邮箱已遮挡/)
assert.match(privacy,/图片数据已省略/)
assert.equal(canExportExtractionAuditReport(audit,true,false,false,''),true)
for(const state of [[null,true,false,false,''],[audit,false,false,false,''],[audit,true,true,false,''],
  [audit,true,false,true,''],[audit,true,false,false,'本轮失败']]){
  assert.equal(canExportExtractionAuditReport(...state),false,'Only a current successful result may be exported')
}

let capturedBlob,clicked=0,removed=0,revoked=[]
const anchor={href:'',download:'',click(){clicked++},remove(){removed++}}
downloadExtractionAuditReport(audit,{
  createObjectURL(blob){capturedBlob=blob;return 'blob:fixture'},
  revokeObjectURL(url){revoked.push(url)},createAnchor(){return anchor},
})
assert.equal(capturedBlob.type,'application/json;charset=utf-8')
assert.equal(await capturedBlob.text(),serialized)
assert.equal(anchor.href,'blob:fixture')
assert.equal(anchor.download,'zhida-extraction-audit.json')
assert.equal(clicked,1)
assert.equal(removed,1)
assert.deepEqual(revoked,['blob:fixture'])
assert.throws(()=>downloadExtractionAuditReport(audit,{
  createObjectURL(){return 'blob:failure'},revokeObjectURL(url){revoked.push(url)},
  createAnchor(){throw Error('fixture cannot create download')},
}),/fixture cannot create/)
assert.equal(revoked.at(-1),'blob:failure','Failed download still releases the local URL')
let copied
await copyExtractionAuditReport(audit,{async writeText(value){copied=value}})
assert.equal(copied,serialized,'Copy and download contain identical evidence')
await assert.rejects(()=>copyExtractionAuditReport(audit),/无法访问剪贴板.*下载 JSON/)
await assert.rejects(()=>copyExtractionAuditReport(audit,{async writeText(){throw Error('forbidden clipboard error body')}}),/复制检查报告失败.*下载 JSON/)

const {ExtractionAuditReportActions}=await bundle('./ExtractionAudit.tsx')
const renderActions=props=>renderToStaticMarkup(createElement(ExtractionAuditReportActions,{
  available:true,previous:false,feedback:null,onCopy(){},onDownload(){},...props,
}))
const currentHtml=renderActions({})
assert.match(currentHtml,/下载本轮检查报告/)
assert.match(currentHtml,/复制检查报告/)
assert.doesNotMatch(currentHtml,/disabled=""/)
const staleHtml=renderActions({available:false,previous:true})
assert.equal((staleHtml.match(/disabled=""/g)||[]).length,2)
assert.match(staleHtml,/上次检查结果，不能作为本轮报告导出/)
const failedHtml=renderActions({feedback:{message:'复制检查报告失败，请下载 JSON 检查报告。',failed:true}})
assert.match(failedHtml,/role="alert"/)
const source=await readFile(new URL('./extractionAuditReport.ts',import.meta.url),'utf8')
assert.doesNotMatch(source,/fetch\(|api\.|localStorage|sessionStorage|indexedDB|snapshot[.:]|resumeId/,'Report generation stays local and accepts only result evidence')
console.log('extractionAuditReport: OK (current result only, complete coverage, safe JSON, local download/copy, clear clipboard failure)')
