import type {ApplicationAssistRequest, ApplicationAssistResult, ApplicationAssistProgress, BrowserSnapshot, ExecutionResult, FormPlan, PageField} from './types'

export type AssistRunReference={session:string;id:string;started:number}
type RunStorage=Pick<Storage,'getItem'|'setItem'|'removeItem'>
const runKey=(owner:string)=>`zhida.assist.run.${encodeURIComponent(owner)}`
const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

export function rememberAssistRun(storage:RunStorage,owner:string,run:AssistRunReference){
  // Metadata only; no applicant answers, selectors, credentials or write plans.
  try{storage.setItem(runKey(owner),JSON.stringify({session:run.session,id:run.id,started:run.started}))}catch{/* unavailable storage must not replay a write */}
}

export function forgetAssistRun(storage:RunStorage,owner:string){
  try{storage.removeItem(runKey(owner))}catch{}
}

export function rememberedAssistRun(storage:RunStorage,owner:string,hash='',now=Date.now()):AssistRunReference|null{
  try{
    // A receipt link only performs owner-checked GETs. It never resumes POSTs.
    const params=new URLSearchParams(hash.replace(/^#/,''))
    const session=params.get('assist-session'),id=params.get('assist-run')
    if(session&&id&&uuid.test(session)&&uuid.test(id))return {session,id,started:now}
    const run=JSON.parse(storage.getItem(runKey(owner))||'null')
    if(run&&uuid.test(run.session)&&uuid.test(run.id)&&Number.isFinite(run.started)&&now>=run.started&&now-run.started<60*60*1000)return {session:run.session,id:run.id,started:run.started}
    forgetAssistRun(storage,owner)
  }catch{}
  return null
}

export function assistRequest(resumeId:string, allowSiteParse=false, deferredFields:PageField[]=[], deferGovernmentId=false):ApplicationAssistRequest {
  return {resume_id:resumeId,allow_site_parse:allowSiteParse,use_model:true,max_rounds:5,
    ...(deferredFields.length?{deferred_fields:deferredFields}:{}),
    ...(deferGovernmentId?{defer_government_id:true}:{})}
}

// Polling must never restart the POST: a lost response is not proof that no
// fields were written. Progress reads do not inspect or mutate the browser.
export async function watchAssist(read:()=>Promise<ApplicationAssistProgress>, update:(p:ApplicationAssistProgress)=>void,
                                  active:()=>boolean, pause:()=>Promise<void>=()=>new Promise(r=>setTimeout(r,1500)),
                                  now:()=>number=Date.now) {
  let misses=0
  let hasReceipt=false
  const started=now()
  while(active()){
    await pause()
    if(!active())return null
    if(now()-started>20*60*1000)throw new Error('本轮结果等待超时，请先只读同步核对；不会自动重发填写。')
    let p:ApplicationAssistProgress
    try{
      p=await read();hasReceipt=true;misses=0;update(p)
    }catch(error){
      const status=error instanceof Error?(error as Error&{status?:number}).status:undefined
      if(status===401||status===403)throw error
      // The POST can queue behind an existing browser observation. A receipt
      // is registered only once it acquires that lock. Keep reading the SAME
      // UUID for a bounded startup grace; never resend the write request.
      if(!hasReceipt&&status===404&&now()-started<90*1000)continue
      if(++misses>=4)throw error
      continue
    }
    if(p.status==='finished')return p.result
    if(p.status==='interrupted')throw new Error(p.message)
  }
  return null
}

export function assistFailure(error:unknown, receiptError:unknown):unknown {
  // A server refusal contains the real guard diagnostic. The generic receipt
  // must not hide it. For a lost connection, use the receipt's known outcome.
  return error instanceof Error&&typeof (error as Error&{status?:unknown}).status==='number'
    ? error : receiptError||error
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
    return Boolean(old&&next&&identity.every(key=>old[key]===next[key])&&JSON.stringify(old.options)===JSON.stringify(next.options)&&
      JSON.stringify((old.question_candidates||[]).filter(item=>item.owned))===JSON.stringify((next.question_candidates||[]).filter(item=>item.owned))&&
      JSON.stringify(old.constraints||{})===JSON.stringify(next.constraints||{})&&
      JSON.stringify(old.required_evidence||[])===JSON.stringify(next.required_evidence||[]))
  })
}
