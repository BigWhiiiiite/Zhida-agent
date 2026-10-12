import {useEffect,useRef,useState} from 'react'
import {api} from './api'
import {previewControlLabel,previewQuestionClear,previewReasonLabel,previewScopeKey,previewStatusLabel,recognitionPreviewSummary,validateRecognitionPreview} from './recognitionPreviewContract'
import type {RecognitionPreview as Preview,RecognitionPreviewScope} from './recognitionPreviewContract'
import './recognition-preview.css'

export function RecognitionPreviewReport({result,previous=false}:{result:Preview;previous?:boolean}){
  const summary=recognitionPreviewSummary(result),{fields,coverage,issues}=result.recognition
  const questionById=new Map(fields.map((field,index)=>[field.id,{field,number:index+1}]))
  return <div className="recognition-preview-report">
    <p className="recognition-preview-notice">{previous?'页面状态已变化，以下仅保留上次快照，不能作为当前识别结果。':'以下仅代表采集时的页面快照；后续切页、展开或修改后请重新检查。'}</p>
    <p><strong>采集时间：</strong><time dateTime={result.captured_at}>{new Date(result.captured_at).toLocaleString()}</time></p>
    <p>页面：{result.title||'标题未读取'} · {summary.coverage}</p>
    <p>题目 {summary.total} 项 · 证据清晰 {summary.clear} 项 · 需核对 {summary.attention} 项</p>
    <p>读取控件 {coverage.captured_controls} 个 · 未映射 {coverage.unmapped_controls} 个 · 未读取嵌入区域 {coverage.embedded_regions} 个 · 未读取 Shadow DOM 区域 {coverage.unread_shadow_regions} 个</p>
    {summary.total===0&&<p role="alert">未读到可核对的题目，不能认定识别成功。</p>}
    {coverage.pending_sections.length>0&&<p>未读取或未展开栏目：{coverage.pending_sections.join('、')}</p>}
    <ul>{coverage.limitations.map((item,index)=><li key={index}>{item}</li>)}</ul>
    {issues.length>0&&<details><summary>查看采集缺口（{issues.length}）</summary><ul className="recognition-preview-issues">{issues.map((issue,index)=>{
      const related=questionById.get(issue.field_id)
      return <li key={index}>
        <strong>{related?`${related.number}. ${related.field.question||'题干缺失，不能要求用户猜测'}`:issue.field_id?'题目未关联':'页面覆盖范围'}</strong>
        {related&&<div>栏目：{related.field.section_path.join(' / ')||'栏目归属未核实'}</div>}
        <div>{issue.detail}</div>
        {issue.field_id&&<small>题目 ID：{issue.field_id}</small>}
      </li>
    })}</ul></details>}
    <div className="recognition-preview-fields">{fields.map((field,index)=><details key={field.id}>
      <summary><span>{index+1}. {field.question||'题干缺失，不能要求用户猜测'}</span><small>{previewQuestionClear(field)?'证据清晰':'需核对'}</small></summary>
      <p>栏目：{field.section_path.join(' / ')||'栏目归属未核实'}</p>
      <dl><div><dt>填写方式</dt><dd>{previewControlLabel(field.control_kind)}</dd></div>
        <div><dt>题干证据</dt><dd>{previewStatusLabel(field.question_status)}</dd></div>
        <div><dt>必填证据</dt><dd>{previewStatusLabel(field.required_status)}</dd></div>
        <div><dt>选项覆盖</dt><dd>{previewStatusLabel(field.options_status)}</dd></div>
        <div><dt>经历归属</dt><dd>{previewStatusLabel(field.entity_binding.status)}</dd></div></dl>
      {field.options.length>0&&<p>读到的真实选项：{field.options.map(option=>`${option.label}${option.disabled?'（禁用）':''}`).join('、')}</p>}
      {field.reasons.length>0&&<ul>{field.reasons.map((reason,i)=><li key={i}>{previewReasonLabel(reason)}</li>)}</ul>}
      <small>题目 ID：{field.id}</small>
    </details>)}</div>
    <small>本次未调用模型、未运行 LangGraph、未持久保存报告；未填写、上传或提交。题干和选项仍可能含个人资料。</small>
  </div>
}

export default function RecognitionPreview({sessionId,resumeId,url,disabled,stale,onBusy,notify}:{
  sessionId:string;resumeId:string;url:string;disabled:boolean;stale:boolean;onBusy:(value:boolean)=>void;notify:(message:string)=>void
}){
  const scope:RecognitionPreviewScope={sessionId,resumeId,url},key=previewScopeKey(scope)
  const scopeRef=useRef({key,stale});scopeRef.current={key,stale}
  const generation=useRef(0),pendingRef=useRef(false)
  const [pending,setPending]=useState(false),[error,setError]=useState('')
  const [report,setReport]=useState<{key:string;result:Preview}|null>(null)
  useEffect(()=>{
    generation.current++;setReport(null);setError('')
    return()=>{generation.current++}
  },[key,stale])
  const inspect=async()=>{
    if(disabled||stale||pendingRef.current||!resumeId)return
    const current=generation.current,requestKey=key
    pendingRef.current=true;setPending(true);setReport(null);setError('');onBusy(true)
    try{
      const response=await api.recognitionPreview(sessionId,{expected_url:url,resume_id:resumeId})
      if(current!==generation.current||scopeRef.current.key!==requestKey||scopeRef.current.stale)return
      const result=validateRecognitionPreview(response,scope)
      setReport({key:requestKey,result})
      const summary=recognitionPreviewSummary(result)
      notify(`D2 只读识别：${summary.total} 项题目，${summary.attention} 项需核对；未调用模型或填写。`)
    }catch(cause){
      if(current===generation.current&&scopeRef.current.key===requestKey&&!scopeRef.current.stale){
        const message=cause instanceof Error?cause.message:'只读识别检查未完成'
        setError(message);notify(message)
      }
    }finally{pendingRef.current=false;setPending(false);onBusy(false)}
  }
  return <section className="card recognition-preview" aria-label="D2 只读识别检查">
    <h3>只读识别检查（D2）</h3>
    <p>在已连接的招聘页读取题干、控件和选项，使用新识别规则核对证据。不会打开新窗口、登录、匹配简历或填写；不会发送模型。</p>
    <button className="secondary" disabled={disabled||stale||pending||!resumeId} onClick={()=>void inspect()}>{pending?'正在读取并核对…':'只读识别检查（D2）'}</button>
    {!resumeId&&<p>请先选择本次简历，以核对当前任务范围；本检查不会读取简历内容。</p>}
    {stale&&<p role="alert">页面已有变化，请先只读同步当前页，再进行检查。</p>}
    {error&&<p className="recognition-preview-notice" role="alert">本轮检查未完成：{error}</p>}
    {report?.key===key&&<RecognitionPreviewReport result={report.result} previous={stale}/>}
  </section>
}
