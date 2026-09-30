import { Check, LoaderCircle, Sparkles } from 'lucide-react'
import type { FormPlan } from './types'
import { modelPending } from './autofillRouting'
import './autofill-routing.css'

export default function AutofillRoutingSummary({plan,busy,userQuestions,phaseProgress}:{plan:FormPlan|null;busy:string;userQuestions:number;phaseProgress:{rules:number|null;model:number|null}}) {
  const summary=plan?.routing_summary
  const pending=modelPending(plan)
  return <div className="autofill-routing" aria-live="polite">
    <div className={busy==='autofill-rules'?'active':''}><b>01</b><span><strong>规则先填</strong><small>{phaseProgress.rules!==null?`本轮 ${phaseProgress.rules} 项已回读验证`:summary?`${summary.rules_ready} 项有确定依据`:'主档案、已确认记忆与安全规则'}</small></span>{busy==='autofill-rules'?<LoaderCircle className="spin" size={17}/>:phaseProgress.rules!==null?<Check size={17}/>:null}</div>
    <div className={busy==='autofill-model'?'active':''}><b>02</b><span><strong>模型处理歧义</strong><small>{phaseProgress.model!==null?`本轮 ${phaseProgress.model} 项已回读验证`:summary?`${summary.model_resolved} 项已解释 · ${pending} 项待分析`:'只分析仍无法确定的字段'}</small></span>{busy==='autofill-model'?<LoaderCircle className="spin" size={17}/>:<Sparkles size={17}/>}</div>
    <div><b>03</b><span><strong>最后请你确认</strong><small>{plan?`${userQuestions} 道问题等待你的事实或决定`:'缺少的个人事实、偏好和敏感决定'}</small></span></div>
    {pending>0&&<p>{busy==='autofill-model'?'规则填写已经完成，模型正在处理剩余项；这些暂不需要你逐个回答。':`还有 ${pending} 个字段待模型分析，不计入下方人工问题。点击“智能分层填写”按顺序继续。`}</p>}
  </div>
}
