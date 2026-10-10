import {useEffect,useRef,useState} from 'react'
import {ApiError,api} from './api'
import {extractionAuditSummary,extractionControlLabel,extractionQuestionNeedsAttention,extractionStatusLabel,requestExtractionAudit} from './extractionAuditReadOnly'
import {canExportExtractionAuditReport,copyExtractionAuditReport,downloadExtractionAuditReport} from './extractionAuditReport'
import type {BrowserSnapshot,ExtractionAuditQuestion,ExtractionAuditResult,FormExtractionReport,ObservationConsent} from './types'
import './extraction-audit.css'

export function ExtractionAuditCoverage({coverage}:{coverage:FormExtractionReport}){
  return <div aria-label="本次只读审计的页面覆盖范围">
    <strong>本次读取范围与缺口</strong>
    <p>本次已采集 {coverage.captured_controls} 个控件 · 未映射 {coverage.unmapped_controls} 个
      {' · '}未读取嵌入区域 {coverage.embedded_regions} 个 · 未读取 Shadow DOM 区域 {coverage.unread_shadow_regions} 个</p>
    {coverage.capture_status!=='observed'&&<p>{coverage.capture_status==='partial'?'当前文档读取不完整，模型即使审阅完已采集题目，也没有看到所有需要填写的信息。':'本次读取范围尚未核实，不能判定整表完整。'}</p>}
    <ul>
      {coverage.embedded_regions>0&&<li>还有 {coverage.embedded_regions} 个嵌入网页区域未读取，里面可能有其他表单或验证控件。</li>}
      {coverage.unread_shadow_regions>0&&<li>还有 {coverage.unread_shadow_regions} 个 Shadow DOM 区域未读取，组件内部的题干或控件可能漏读。</li>}
      {coverage.pending_sections.map((title,index)=><li key={index}>未展开或未激活栏目：{title}</li>)}
    </ul>
    <small>以上直接取自本次审计结果，不是上次页面快照。没有报告上述缺口，也不等于跨页或以后动态出现的题目已经读全。</small>
  </div>
}

export function ExtractionAuditReportActions({available,previous,feedback,onDownload,onCopy}:{
  available:boolean;previous:boolean;feedback:{message:string;failed:boolean}|null;
  onDownload:()=>void;onCopy:()=>void
}){
  return <div aria-label="导出本轮只读检查报告">
    <button className="secondary" disabled={!available} onClick={onDownload}>下载本轮检查报告</button>
    <button className="secondary" disabled={!available} onClick={onCopy}>复制检查报告</button>
    {previous&&<p>下方是上次检查结果，不能作为本轮报告导出。重新完成检查后可导出。</p>}
    {!previous&&!available&&<p>检查进行中或页面尚未同步，暂不能导出本轮报告。</p>}
    {feedback&&<p role={feedback.failed?'alert':'status'}>{feedback.message}</p>}
    <small>导出当前检查结果中的题目、覆盖缺口和模型依据，不包含截图文件、账户信息或当前填写值。</small>
  </div>
}

function QuestionEvidence({question,index}:{question:ExtractionAuditQuestion;index:number}){
  const review=question.model_review
  const verdict=!review?'模型未审阅':review.verdict==='clear'?'模型认为题意清晰':review.verdict==='conflict'?'模型意见或引文需核对':'需要补充观察'
  return <details className="extraction-audit-question">
    <summary><span>{index+1}. {question.title||'题干缺失，不能要求用户猜测填写'}</span><small>{verdict}</small></summary>
    <p>栏目：{question.section_path.join(' / ')||'栏目归属未核实'}</p>
    <dl><div><dt>控件方式</dt><dd>{extractionControlLabel(question.control_kind)}</dd></div>
      <div><dt>必填证据</dt><dd>{extractionStatusLabel(question.required_status)}</dd></div>
      <div><dt>选项覆盖</dt><dd>{extractionStatusLabel(question.options_status)}</dd></div>
      <div><dt>经历归属</dt><dd>{extractionStatusLabel(question.record_status)}</dd></div></dl>
    {question.observed_options.length>0&&<p>实际读取的选项：{question.observed_options.join('、')}</p>}
    {question.issues.length>0&&<><strong>采集发现的问题</strong><ul>{question.issues.map((item,i)=><li key={i}>{item}</li>)}</ul></>}
    {review?<><p><strong>模型如何理解这道题：</strong>{review.interpretation||'未给出解释'}</p>
      <p>模型判断的控件：{extractionControlLabel(review.control_kind)}（仅为审阅意见，不覆盖原始采集证据）</p>
      {review.issue&&<p>模型发现的问题：{review.issue}</p>}
      {review.next_observation&&<p>建议补读：{review.next_observation}</p>}
      {review.evidence.length>0&&<><strong>模型采用的网页依据</strong><ul>{review.evidence.map((item,i)=><li key={i}>{item}</li>)}</ul></>}
    </>:<p>模型没有实际审阅这道题，不能把本地识别结果算作模型已理解。</p>}
    <details><summary>查看题目定位信息（不是填写答案）</summary><small>题目 ID：{question.question_id}</small><pre>{question.selectors.join('\n')}</pre></details>
  </details>
}

export default function ExtractionAudit({snapshot,resumeId,revision,disabled,onBusy,notify,onPageInvalidated}:{
  snapshot:BrowserSnapshot;resumeId:string;revision:string;disabled:boolean;
  onBusy:(busy:boolean)=>void;notify:(message:string)=>void
  onPageInvalidated?:()=>void
}){
  const [consent,setConsent]=useState<ObservationConsent|null>(null)
  const [inspectControls,setInspectControls]=useState(false)
  const [includeImages,setIncludeImages]=useState(false)
  const [useModel,setUseModel]=useState(true)
  const [pending,setPending]=useState(false)
  const [result,setResult]=useState<ExtractionAuditResult|null>(null)
  const [resultCurrent,setResultCurrent]=useState(false)
  const [reportFeedback,setReportFeedback]=useState<{message:string;failed:boolean}|null>(null)
  const [onlyIssues,setOnlyIssues]=useState(false)
  const [error,setError]=useState('')
  const generation=useRef(0)
  const reportEpoch=useRef(0)
  useEffect(()=>{
    const current=++generation.current
    reportEpoch.current++
    setConsent(null);setResult(null);setResultCurrent(false);setReportFeedback(null);setError('');setIncludeImages(false)
    api.observationConsent(snapshot.session_id).then(value=>{
      if(generation.current===current)setConsent(value)
    }).catch(()=>{/* Clicking the audit button will perform a fresh, explicit connectivity check. */})
    return ()=>{generation.current++}
  },[snapshot.session_id,snapshot.url,resumeId,revision])
  const run=async(operation:(current:number)=>Promise<void>)=>{
    if(disabled||pending)return
    const current=generation.current
    setPending(true);setError('');onBusy(true)
    try{await operation(current)}catch(cause){
      if(current===generation.current&&cause instanceof ApiError&&[404,409].includes(cause.status))onPageInvalidated?.()
      if(current===generation.current){const text=cause instanceof Error?cause.message:'只读检查未完成';setError(text);notify(text)}
    }finally{setPending(false);onBusy(false)}
  }
  const check=()=>run(async(current)=>{
    reportEpoch.current++;setResultCurrent(false);setReportFeedback(null)
    const value=await requestExtractionAudit(snapshot.session_id,{useModel,inspectControls,includeImages},
      {consent:api.observationConsent,audit:api.extractionAudit})
    if(current!==generation.current)return
    setResult(value);setResultCurrent(true)
    notify(`${extractionAuditSummary(value).status}；没有填写、上传、保存或提交。`)
  })
  const reportAvailable=canExportExtractionAuditReport(result,resultCurrent,disabled,pending,error)
  const downloadReport=()=>{
    if(!result||!reportAvailable)return
    try{
      downloadExtractionAuditReport(result,{
        createObjectURL:blob=>URL.createObjectURL(blob),revokeObjectURL:url=>{window.setTimeout(()=>URL.revokeObjectURL(url),1000)},
        createAnchor:()=>{const anchor=document.createElement('a');anchor.hidden=true;document.body.appendChild(anchor);return anchor},
      })
      setReportFeedback({message:'已下载本轮 JSON 检查报告。',failed:false})
    }catch{setReportFeedback({message:'下载检查报告失败，可尝试复制检查报告。',failed:true})}
  }
  const copyReport=async()=>{
    if(!result||!reportAvailable)return
    const current=generation.current,epoch=reportEpoch.current
    try{
      await copyExtractionAuditReport(result,navigator.clipboard)
      if(current===generation.current&&epoch===reportEpoch.current)setReportFeedback({message:'已复制本轮 JSON 检查报告。',failed:false})
    }catch(cause){
      if(current===generation.current&&epoch===reportEpoch.current)setReportFeedback({
        message:cause instanceof Error?cause.message:'复制检查报告失败，请下载 JSON 检查报告。',failed:true,
      })
    }
  }
  const authorizeImages=(enabled:boolean)=>run(async(current)=>{
    const latest=await api.observationConsent(snapshot.session_id)
    const value=await api.setObservationConsent(snapshot.session_id,enabled,latest.context_token)
    if(current!==generation.current)return
    setConsent(value);setIncludeImages(enabled&&value.enabled)
  })
  const summary=result?extractionAuditSummary(result):null
  const questions=result?.questions.filter(q=>!onlyIssues||extractionQuestionNeedsAttention(q))||[]
  return <section className="card extraction-audit" aria-label="整页识别只读检查">
    <h3>先核对职达到底读到了什么</h3>
    <p>将当前页面采集到的全部题目交给模型核对题干、填写方式、真实选项和经历归属。这不是填写计划，不使用简历答案，也不会修改招聘表单。</p>
    <fieldset disabled={disabled||pending}>
      <label><input type="checkbox" checked={useModel} onChange={event=>{setUseModel(event.target.checked);if(!event.target.checked)setIncludeImages(false)}}/>让已配置模型审阅全部已采集题目</label>
      <label><input type="checkbox" checked={inspectControls} onChange={event=>setInspectControls(event.target.checked)}/>允许安全打开选择菜单补读选项（不选择、不填写；不展开新增经历）</label>
      <details><summary>可选：授权模型查看遮挡后的题目画面</summary>
        <label><input type="checkbox" checked={consent?.enabled??false} onChange={event=>void authorizeImages(event.target.checked)}/>允许向已配置 API 服务发送本轮遮挡后的题目截图</label>
        <label><input type="checkbox" checked={includeImages} disabled={!consent?.enabled||!useModel} onChange={event=>setIncludeImages(event.target.checked)}/>本次检查使用已授权截图</label>
        <small>默认不发图片。授权仅适用于当前会话和资料版本；题干仍可能含个人资料，遮挡不等于完全匿名。变更页面或资料后需重新核实。</small>
      </details>
      <button className="secondary" onClick={()=>void check()}>{pending?'正在只读检查，未执行填写…':'只读检查整页识别（不填写）'}</button>
    </fieldset>
    <small>范围仅限当前已经呈现的网页；未展开栏目、跨页表单和嵌入区域会列为缺口，不会宣称整张申请表读全。结果不会清除你的临时填写答案。</small>
    {error&&<p className="extraction-audit-error" role="alert">本轮检查未完成：{error}{result?'。下方保留上次结果，不是本轮成功结果。':''}</p>}
    {result&&summary&&<div className="extraction-audit-result" role="status">
      <ExtractionAuditReportActions available={reportAvailable} previous={!resultCurrent||!!error} feedback={reportFeedback}
        onDownload={downloadReport} onCopy={()=>void copyReport()}/>
      <strong>{summary.status}</strong>
      <p>采集 {result.total_questions} 道题 · 模型实际审阅 {summary.reviewed} 道
        {summary.unreviewed>0&&` · 未审阅 ${summary.unreviewed} 道`} · 待核实 {summary.attention} 道</p>
      <p>模型：{result.model_name||'本轮未调用'} · 模型批次 {result.model_batches}
        {' · '}补读收到 {result.observations_received}/{result.observations_requested} 次 · 提供截图 {result.images_supplied} 张</p>
      <ExtractionAuditCoverage coverage={result.coverage}/>
      {result.limitations.length>0&&<><strong>本轮限制与未覆盖部分</strong><ul>{result.limitations.map((item,i)=><li key={i}>{item}</li>)}</ul></>}
      <label><input type="checkbox" checked={onlyIssues} onChange={event=>setOnlyIssues(event.target.checked)}/>只看有问题或模型未审阅的题目</label>
      <div className="extraction-audit-questions">{questions.map(question=><QuestionEvidence key={question.question_id} question={question} index={result.questions.indexOf(question)}/>)}</div>
      {questions.length===0&&<p>{result.total_questions===0?'当前没有采集到填写题目，不能判定识别成功。':'此筛选下没有题目；可取消筛选查看模型实际审阅的全部题目。'}</p>}
      <details><summary>查看模型输入范围清单</summary><pre>{JSON.stringify(result.input_manifest,null,2)}</pre></details>
      <small>本轮只读检查没有填写、上传、保存或提交，也没有替用户选择答案。</small>
    </div>}
  </section>
}
