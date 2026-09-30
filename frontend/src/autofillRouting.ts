import type { AutofillPhaseResult, ExecutionResult, FillAction, FormPlan, PageField } from './types'

export function actionReviewTitle(action:FillAction|undefined, field?:PageField, fallback='') {
  return action?.review_question?.trim() || field?.question_text || field?.group_label || field?.label || action?.label || fallback || '请先定位网页核对这道题'
}

export function routeLabel(action:FillAction, manual=false) {
  if(manual || action.user_confirmed) return '用户已确认'
  if(action.needs_model) return '待模型分析'
  if(action.resolution_source==='blocked') return '安全边界 · 人工处理'
  if(action.resolution_source==='user') return '需要你的答案'
  if(action.resolution_source==='model') return '模型语义分析'
  return ['fill','select','check'].includes(action.action) ? '规则 / 已确认资料' : '人工核对'
}

export function modelPending(plan:FormPlan|null) {
  return plan?.routing_summary?.model_pending ?? plan?.actions.filter(action=>action.needs_model).length ?? 0
}

// Preserve the first phase's audit results; the latest attempt wins for a field.
// These are execution records, not a claim that untouched dynamic fields were re-read.
export function mergePhaseExecutions(previous:ExecutionResult|null, next:ExecutionResult):ExecutionResult {
  if(!previous) return next
  const bySelector=new Map(previous.results.map(result=>[result.selector,result]))
  next.results.forEach(result=>bySelector.set(result.selector,result))
  const results=[...bySelector.values()]
  return {...next,results,completed:results.filter(result=>result.status==='filled').length,
    skipped:results.filter(result=>result.status==='skipped').length,
    failed:results.filter(result=>result.status==='failed').length,
    verified:results.filter(result=>result.status==='filled'&&result.verified).length,
    unverified:results.filter(result=>result.status==='filled'&&!result.verified).length}
}

export async function runAutofillPhases(
  request:(phase:'rules'|'model')=>Promise<AutofillPhaseResult>,
  onPhase:(result:AutofillPhaseResult,execution:ExecutionResult)=>void,
  onModelStart:()=>void,
) {
  const first=await request('rules')
  let latest=first
  let execution=first.execution
  onPhase(first,execution)
  if(modelPending(first.review.plan)>0){
    onModelStart()
    latest=await request('model')
    execution=mergePhaseExecutions(execution,latest.execution)
    onPhase(latest,execution)
  }
  return {first,latest,execution}
}
