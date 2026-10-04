import type {ApplicationAssistRequest, ApplicationAssistResult, BrowserSnapshot, ExecutionResult, FormPlan, PageField} from './types'

export function assistRequest(resumeId:string, allowSiteParse=false, deferredFields:PageField[]=[], deferGovernmentId=false):ApplicationAssistRequest {
  return {resume_id:resumeId,allow_site_parse:allowSiteParse,use_model:true,max_rounds:3,
    ...(deferredFields.length?{deferred_fields:deferredFields}:{}),
    ...(deferGovernmentId?{defer_government_id:true}:{})}
}

export function assistanceBackendReady(current:{resume_id?:string;assistance_version?:number}):boolean {
  return typeof current.resume_id==='string'&&typeof current.assistance_version==='number'&&current.assistance_version>=1
}

type AssistGuard = {busy:boolean;backendReady:boolean;hasSnapshot:boolean;formReady:boolean;stalePage:boolean;hasDraftEdits:boolean;resumeId:string;planResumeId?:string}
export function assistGuard(state:AssistGuard):string {
  if(state.busy)return '正在处理上一项操作，请稍候。'
  if(!state.backendReady)return '本轮自动补齐升级尚未加载，请先保留招聘草稿，再重启后端。'
  if(!state.hasSnapshot||!state.formReady)return '请先进入招聘网站的申请表单，再同步当前页面。'
  if(state.stalePage)return '招聘网页已变化，请先同步当前页面；旧填写计划不会继续执行。'
  if(state.hasDraftEdits)return '本页有你的临时答案或“不填写”选择，请先填写并验证，或明确清除临时修改。'
  if(!state.resumeId)return '请先选择本次投递使用的简历，避免混用不同版本的经历。'
  if(state.planResumeId!==undefined&&state.planResumeId!==state.resumeId)return '分析计划与当前简历不一致，请同步当前页面后重新分析。'
  return ''
}

export function assistStatus(status:ApplicationAssistResult['status']) {
  switch(status){
    case 'ready_for_review':return {title:'已完成本轮补齐，请人工终审',tone:'ready',detail:'请在招聘网站核对岗位、经历和附件；协议及最终提交由你决定。'}
    case 'needs_user':return {title:'还需要你的确认',tone:'pending',detail:'有缺少或不确定的信息。请处理下方问题，再运行自动补齐。'}
    case 'blocked':return {title:'已安全暂停，尚未完成填写',tone:'blocked',detail:'请先处理下方阻塞原因，系统不会继续硬填。'}
    case 'partial':return {title:'已完成部分步骤，仍需继续核对',tone:'pending',detail:'本轮没有完成全部步骤。已填写的内容保留，请核对剩余项后继续。'}
  }
}

export function needsMonthPrecisionConsent(url:string,plan:FormPlan|null):boolean {
  try{const parsed=new URL(url);if(parsed.protocol!=='https:'||parsed.hostname!=='talent.autohome.com.cn')return false}catch{return false}
  return Boolean(plan?.actions.some(action=>/年月精度|仅有年月|不会擅补1日/.test(`${action.reason} ${action.review_hint||''}`)))
}

export const monthPrecisionMemory = {
  question:'年月精度日期的网申填写约定',
  value:'每月1日作为月份占位，不代表真实精确日期；至今保留',
  semantic_key:'application.date_precision',
  entity_scope:'application',
}

// Learning after an execution must be based on read-back, not on the attempted
// values. Explicit "remember" remains a separate user-confirmed operation.
export function verifiedDraftEntries(answers:Record<string,string>,execution:ExecutionResult):[string,string][] {
  const latest=new Map(execution.results.map(result=>[result.selector,result]))
  return Object.entries(answers).filter(([selector,value])=>{
    const result=latest.get(selector)
    return Boolean(value.trim()&&result?.status==='filled'&&result.verified)
  })
}

export async function learnVerifiedDrafts(answers:Record<string,string>,execution:ExecutionResult,persist:(selector:string,value:string)=>Promise<boolean>):Promise<{count:number;failed:string[]}> {
  let count=0;const failed:string[]=[]
  for(const [selector,value] of verifiedDraftEntries(answers,execution)){
    try{if(await persist(selector,value))count+=1}catch{failed.push(selector)}
  }
  return {count,failed}
}

export function draftFieldsCompatible(selectors:string[],before:BrowserSnapshot,after:BrowserSnapshot):boolean {
  if(before.session_id!==after.session_id||before.url!==after.url)return false
  const identity=['field_type','field_signature','question_text','label','name','semantic_key','entity_scope','container_key','control_group_key','section','group_label','option_label','option_value'] as const
  return selectors.every(selector=>{
    const old=before.fields.find(field=>field.selector===selector)
    const next=after.fields.find(field=>field.selector===selector)
    return Boolean(old&&next&&identity.every(key=>old[key]===next[key])&&JSON.stringify(old.options)===JSON.stringify(next.options))
  })
}
