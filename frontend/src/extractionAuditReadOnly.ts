import type {ExtractionAuditQuestion,ExtractionAuditRequest,ExtractionAuditResult,ObservationConsent} from './types'

export type ExtractionAuditOptions = {useModel:boolean;inspectControls:boolean;includeImages:boolean}
export type ExtractionAuditOperations = {
  consent:(sessionId:string)=>Promise<ObservationConsent>;
  audit:(sessionId:string,payload:ExtractionAuditRequest)=>Promise<ExtractionAuditResult>;
}

// This deliberately has no navigation, profile, planning, execution, or filling dependency.
// Obtain the revision immediately before the request rather than reuse a previous plan.
export async function requestExtractionAudit(sessionId:string,options:ExtractionAuditOptions,operations:ExtractionAuditOperations){
  const consent=await operations.consent(sessionId)
  if(options.includeImages&&!options.useModel)throw new Error('仅本地检查不需要发送模型截图，请关闭截图选项。')
  if(!consent.context_token)throw new Error('当前只读检查版本尚未获取，请先只读同步当前页。')
  if(options.includeImages&&!consent.enabled)throw new Error('当前页面的截图授权尚未启用或已失效。请重新授权后检查，未发送截图。')
  const result=await operations.audit(sessionId,{
    context_token:consent.context_token,use_model:options.useModel,
    inspect_controls:options.inspectControls,include_images:options.includeImages,
  })
  if(result.read_only!==true||result.session_id!==sessionId||result.scope!=='current_visible_document'){
    throw new Error('只读检查返回的会话或范围不匹配，结果未采纳。')
  }
  return result
}

export function extractionQuestionNeedsAttention(question:ExtractionAuditQuestion){
  return !question.model_review||question.model_review.verdict!=='clear'||question.issues.length>0
    ||['unavailable','deferred','dependent','observed_subset'].includes(question.options_status)
    ||['unresolved','ambiguous'].includes(question.record_status)||question.control_kind==='unknown'
}

export function extractionAuditSummary(result:ExtractionAuditResult){
  const reviewedIds=new Set(result.questions.filter(q=>q.model_review?.question_id===q.question_id).map(q=>q.question_id))
  const reviewed=Math.min(result.total_questions,Math.max(0,result.model_reviewed_questions),reviewedIds.size)
  const unreviewed=Math.max(0,result.total_questions-reviewed)
  const attention=result.questions.filter(extractionQuestionNeedsAttention).length
  const coverage=result.coverage
  const complete=result.total_questions>0&&result.model_status==='complete'&&unreviewed===0
    &&result.questions.length===result.total_questions&&coverage.capture_status==='observed'
    &&coverage.question_count===result.total_questions&&coverage.unmapped_controls===0
    &&coverage.unclear_questions===0&&coverage.options_pending_questions===0
    &&coverage.ambiguous_record_questions===0&&coverage.embedded_regions===0
    &&coverage.unread_shadow_regions===0&&coverage.pending_sections.length===0&&attention===0
  const status=result.model_status==='unavailable'?'模型未完成审阅，仅展示采集证据'
    :result.model_status==='not_requested'?'仅检查采集证据，本轮未调用模型'
    :unreviewed>0?`模型审阅尚未覆盖全部题目，${unreviewed} 道未审阅`
    :complete?'模型已核对当前读取的题目；不代表整张申请表已经读全'
    :'模型已审阅当前读取题目，仍有识别缺口需要核实'
  return {reviewed,unreviewed,attention,complete,status}
}

export const extractionControlLabel=(kind:string)=>({
  text:'直接输入',textarea:'多行输入',select:'下拉选择',custom_select:'自定义下拉',
  native_select:'原生下拉',dropdown:'下拉选择',cascade:'级联选择',section:'栏目展开',radio:'单选',checkbox:'勾选',date:'日期',month:'月份',
  calendar:'日期选择器',region:'地区级联',region_picker:'地区级联',file:'附件上传',unknown:'控件待核实',
} as Record<string,string>)[kind]||kind||'控件待核实'

export const extractionStatusLabel=(status:string)=>({
  required:'有必填依据',not_marked:'未观察到必填标记（不是确定选填）',
  native_complete:'原生选项已读取',group_complete:'同题选项已读取',observed_subset:'仅读取部分选项',
  unavailable:'尚未读取选项',deferred:'选项待展开',dependent:'下级选项依赖上级选择',
  calendar:'日历选项',not_applicable:'不适用',container_observed:'已观察到经历分组',
  unresolved:'经历归属待核实',ambiguous:'经历归属有冲突',
} as Record<string,string>)[status]||status||'待核实'
