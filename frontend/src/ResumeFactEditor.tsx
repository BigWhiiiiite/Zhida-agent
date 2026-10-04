import {useEffect,useRef,useState} from 'react'
import {LoaderCircle,RefreshCw,Save} from 'lucide-react'
import {api,ApiError} from './api'
import {confirmedFactRequest,resumeChoiceLabel} from './resumeFacts'
import type {ResumeFactTargets,ResumeRecord} from './types'

type Props={resume:ResumeRecord;disabled:boolean;onBusy:(busy:boolean)=>void;onSaved:(resume:ResumeRecord)=>Promise<string>}
export default function ResumeFactEditor({resume,disabled,onBusy,onSaved}:Props){
  const [data,setData]=useState<ResumeFactTargets|null>(null)
  const [recordKey,setRecordKey]=useState('')
  const [attribute,setAttribute]=useState('')
  const [value,setValue]=useState('')
  const [busy,setBusy]=useState(false)
  const [preview,setPreview]=useState(false)
  const [notice,setNotice]=useState('')
  const epoch=useRef(0)
  useEffect(()=>()=>{epoch.current+=1},[])
  const record=data?.records.find(item=>item.record_key===recordKey)
  const field=record?.attributes.find(item=>item.key===attribute)
  const load=async()=>{
    if(busy||disabled)return
    const request=++epoch.current;setBusy(true);setNotice('');setPreview(false)
    try{const next=await api.resumeFactTargets(resume.id);if(request!==epoch.current)return;setData(next);setRecordKey('');setAttribute('');setValue('')}
    catch(error){if(request===epoch.current)setNotice(error instanceof Error?error.message:'读取失败，请重试。')}
    finally{if(request===epoch.current)setBusy(false)}
  }
  const inspect=()=>{
    if(!data)return
    try{confirmedFactRequest(data,resume.id,recordKey,attribute,value);setPreview(true);setNotice('')}
    catch(error){setNotice(error instanceof Error?error.message:'请核对输入。')}
  }
  const save=async()=>{
    if(!data||!preview||disabled||busy)return
    let payload
    try{payload=confirmedFactRequest(data,resume.id,recordKey,attribute,value)}catch(error){setNotice(error instanceof Error?error.message:'请核对输入。');return}
    // A separate confirmation step: an explicit record selection never comes from the model.
    setBusy(true);onBusy(true);setNotice('')
    try{
      const updated=await api.confirmResumeFact(resume.id,payload)
      setPreview(false)
      const result=await onSaved(updated)
      setNotice(result)
      setData(null);setRecordKey('');setAttribute('');setValue('')
      try{setData(await api.resumeFactTargets(resume.id))}catch{setNotice(`${result} 最新可编辑资料读取失败，请点击“重新读取”。`)}
    }catch(error){
      if(error instanceof ApiError&&error.status===409){setData(null);setPreview(false);setNotice('简历已被其他操作更新，本次没有覆盖。请重新读取并再次核对。')}
      else setNotice(error instanceof Error?error.message:'保存失败，未确认入库，请重试。')
    }finally{setBusy(false);onBusy(false)}
  }
  return <details className="resume-fact-editor" onToggle={event=>{if(event.currentTarget.open&&!data&&!busy)void load()}}>
    <summary>补充这份简历的真实资料（跨公司复用）</summary>
    <p>只修改 <strong>{resumeChoiceLabel(resume)}</strong> 的结构化资料。请选择具体经历，不会把本科、硕士或不同项目混在一起。其他简历和附件原文件不会自动修改。</p>
    <p>网站下拉选项、排名区间和统招等回答请用“记住本网站答案”；这里用于可核实的个人事实，不会把网站选择当作新事实。</p>
    <button className="secondary" disabled={busy||disabled} onClick={load}>{busy?<LoaderCircle className="spin" size={14}/>:<RefreshCw size={14}/>}重新读取</button>
    {data?.warnings.map((warning,index)=><p role="note" key={index}>{warning}</p>)}
    {data&&!data.records.length&&<p>没有可安全区分的经历记录。请先到简历资料库确认或区分经历。</p>}
    {data&&data.records.length>0&&<fieldset disabled={busy||disabled} className="workbench-action-scope">
      <div className="form-grid two">
        <label className="field"><span>这条事实属于哪段经历？</span><select value={recordKey} onChange={event=>{setRecordKey(event.target.value);setAttribute('');setValue('');setPreview(false)}}><option value="">请明确选择，不自动匹配</option>{data.records.map(item=><option key={item.record_key} value={item.record_key}>{({education:'教育',internships:'实习',projects:'项目'})[item.section]} · {item.label}</option>)}</select></label>
        <label className="field"><span>要补充的属性</span><select value={attribute} disabled={!record} onChange={event=>{setAttribute(event.target.value);setValue('');setPreview(false)}}><option value="">请选择属性</option>{record?.attributes.map(item=><option key={item.key} value={item.key}>{item.label}</option>)}</select></label>
      </div>
      {field&&<><label className="field"><span>{field.label}的真实值</span><textarea rows={3} value={value} onChange={event=>{setValue(event.target.value);setPreview(false)}} placeholder={['start_date','end_date'].includes(attribute)?'例如 2023-12；仅知道年月就保留年月，不补 01':'请输入你确认的真实信息'}/></label><small>当前保存：{field.value||'尚未记录'}</small><div><button className="secondary" onClick={inspect}>预览本次修改</button></div></>}
      {preview&&record&&field&&<div className="fact-preview" role="status"><strong>请再次确认归属与内容</strong><p>简历：{resumeChoiceLabel(resume)}<br/>经历：{record.label}<br/>属性：{field.label}</p><p><span>旧值：{field.value||'尚未记录'}</span><br/><b>新值：{value.trim()}</b></p><small>保存后会使旧填写计划失效，并重新核对；不会立即填写招聘网页或修改 PDF/DOCX 附件。</small><button className="primary" onClick={save}><Save size={14}/>确认真实无误，保存到此简历</button><button className="secondary" onClick={()=>setPreview(false)}>返回修改</button></div>}
    </fieldset>}
    {notice&&<p role="status" className="fact-notice">{notice}</p>}
  </details>
}
