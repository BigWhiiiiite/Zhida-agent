import type {ExtractionAuditResult} from './types'

// Export only answer-free audit evidence. Never serialize the snapshot, profile,
// arbitrary response properties, screenshot bytes or the browser session id.
const safeText=(value:string)=>value
  .replace(/data:image\/[^\s"']+/gi,'[图片数据已省略]')
  .replace(/[A-Za-z0-9+/]{80,}={0,2}/g,'[编码数据已省略]')
  .replace(/[\w.+-]+@[\w.-]+\.[a-z]{2,}/gi,'[邮箱已遮挡]')
  .replace(/\b1[3-9]\d{9}\b/g,'[手机号已遮挡]')
  .replace(/\b\d{17}[\dXx]\b|\b\d{15,19}\b/g,'[证件或账户号码已遮挡]')
const texts=(values:string[])=>values.map(safeText)

export function createExtractionAuditReport(result:ExtractionAuditResult){
  const coverage=result.coverage
  const manifest:Record<string,unknown>={}
  for(const key of ['captured_fields','questions_sent','questions','credential_controls_excluded',
    'unread_embedded_regions','unread_shadow_regions']){
    const value=result.input_manifest[key]
    if(typeof value==='number'&&Number.isFinite(value))manifest[key]=value
  }
  for(const key of ['candidate_profile_sent','current_values_sent','candidate_data_included',
    'all_collected_questions_included','hidden_or_future_questions_covered']){
    const value=result.input_manifest[key]
    if(typeof value==='boolean')manifest[key]=value
  }
  for(const key of ['pending_sections','noninteractive_only_selectors']){
    const value=result.input_manifest[key]
    if(Array.isArray(value)&&value.every(item=>typeof item==='string'))manifest[key]=texts(value)
  }
  if(typeof result.input_manifest.surface_inventory_status==='string'){
    manifest.surface_inventory_status=safeText(result.input_manifest.surface_inventory_status)
  }
  return {
    report_version:1,
    read_only:result.read_only,
    scope:result.scope,
    model_status:result.model_status,
    model_name:safeText(result.model_name),
    total_questions:result.total_questions,
    model_reviewed_questions:result.model_reviewed_questions,
    model_batches:result.model_batches,
    observations_requested:result.observations_requested,
    observations_received:result.observations_received,
    images_supplied:result.images_supplied,
    input_manifest:manifest,
    coverage:{
      version:coverage.version,scope:coverage.scope,capture_status:coverage.capture_status,
      observed_controls:coverage.observed_controls,captured_controls:coverage.captured_controls,
      intentionally_excluded_controls:coverage.intentionally_excluded_controls,
      unmapped_controls:coverage.unmapped_controls,question_count:coverage.question_count,
      verified_questions:coverage.verified_questions,unclear_questions:coverage.unclear_questions,
      options_pending_questions:coverage.options_pending_questions,
      ambiguous_record_questions:coverage.ambiguous_record_questions,
      embedded_regions:coverage.embedded_regions,unread_shadow_regions:coverage.unread_shadow_regions,
      pending_sections:texts(coverage.pending_sections),limitations:texts(coverage.limitations),
      issues:coverage.issues.map(issue=>({label:safeText(issue.label),reason:safeText(issue.reason),selector:safeText(issue.selector)})),
    },
    limitations:texts(result.limitations),
    questions:result.questions.map(question=>({
      question_id:safeText(question.question_id),title:safeText(question.title),
      section_path:texts(question.section_path),selectors:texts(question.selectors),
      control_kind:safeText(question.control_kind),required_status:safeText(question.required_status),
      options_status:safeText(question.options_status),record_status:safeText(question.record_status),
      observed_options:texts(question.observed_options),issues:texts(question.issues),
      model_review:question.model_review?{
        question_id:safeText(question.model_review.question_id),
        interpretation:safeText(question.model_review.interpretation),
        control_kind:safeText(question.model_review.control_kind),verdict:question.model_review.verdict,
        issue:safeText(question.model_review.issue),next_observation:safeText(question.model_review.next_observation),
        evidence:texts(question.model_review.evidence),
      }:null,
    })),
  }
}

export const serializeExtractionAuditReport=(result:ExtractionAuditResult)=>JSON.stringify(createExtractionAuditReport(result),null,2)

export const canExportExtractionAuditReport=(result:ExtractionAuditResult|null,current:boolean,disabled:boolean,pending:boolean,error:string)=>
  Boolean(result&&current&&!disabled&&!pending&&!error)

export type ExtractionAuditReportDownload = {
  createObjectURL:(blob:Blob)=>string;
  revokeObjectURL:(url:string)=>void;
  createAnchor:()=>{href:string;download:string;click:()=>void;remove:()=>void};
}

export function downloadExtractionAuditReport(result:ExtractionAuditResult,download:ExtractionAuditReportDownload){
  const blob=new Blob([serializeExtractionAuditReport(result)],{type:'application/json;charset=utf-8'})
  const url=download.createObjectURL(blob)
  let anchor:ReturnType<ExtractionAuditReportDownload['createAnchor']>|undefined
  try{
    anchor=download.createAnchor()
    anchor.href=url;anchor.download='zhida-extraction-audit.json';anchor.click()
  }finally{
    try{anchor?.remove()}finally{download.revokeObjectURL(url)}
  }
}

export async function copyExtractionAuditReport(result:ExtractionAuditResult,clipboard?:{writeText:(text:string)=>Promise<void>}){
  if(!clipboard)throw new Error('当前环境无法访问剪贴板，请下载 JSON 检查报告。')
  try{await clipboard.writeText(serializeExtractionAuditReport(result))}
  catch{throw new Error('复制检查报告失败，请下载 JSON 检查报告。')}
}
