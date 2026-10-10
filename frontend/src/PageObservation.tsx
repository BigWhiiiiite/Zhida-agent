import {useEffect,useRef,useState} from 'react'
import {api} from './api'
import type {BrowserSnapshot,ObservationConsent,PageRegionObservation} from './types'

export default function PageObservation({snapshot,resumeId,revision,disabled,onBusy,notify}:{
  snapshot:BrowserSnapshot;resumeId:string;revision:string;disabled:boolean;
  onBusy:(value:boolean)=>void;notify:(message:string)=>void
}){
  const [selector,setSelector]=useState('')
  const [previewImage,setPreviewImage]=useState(false)
  const [consent,setConsent]=useState<ObservationConsent|null>(null)
  const [sample,setSample]=useState<PageRegionObservation|null>(null)
  const [pending,setPending]=useState(false)
  const generation=useRef(0)
  const fields=snapshot.fields.filter(f=>!['file','password','hidden','section-button'].includes(f.field_type)
    && !/密码|验证码|同意|承诺|声明|password|captcha|consent|privacy|\botp\b/i.test(`${f.name} ${f.label} ${f.question_text}`))
  useEffect(()=>{
    const current=++generation.current
    setSample(null);setConsent(null);setSelector('');setPreviewImage(false)
    api.observationConsent(snapshot.session_id).then(value=>{
      if(current===generation.current)setConsent(value)
    }).catch(()=>{/* Older backend: leave controls disabled, no API/model retry loop. */})
    return ()=>{generation.current++}
  },[snapshot.session_id,snapshot.url,resumeId,revision])
  const errorMessage=(error:unknown)=>error instanceof Error?error.message:'只读观察未完成'
  const run=async(operation:()=>Promise<void>)=>{
    if(disabled||pending||!consent)return
    setPending(true);onBusy(true)
    try{await operation()}catch(error){notify(errorMessage(error))}finally{setPending(false);onBusy(false)}
  }
  const preview=()=>run(async()=>{
    const current=generation.current
    setSample(null)
    const value=await api.observeRegion(snapshot.session_id,selector,previewImage,consent!.context_token)
    if(current===generation.current)setSample(value)
  })
  const authorize=(enabled:boolean)=>run(async()=>{
    const current=generation.current
    const value=await api.setObservationConsent(snapshot.session_id,enabled,consent!.context_token)
    if(current===generation.current)setConsent(value)
  })
  return <section className="card page-observation" aria-label="题目画面与可访问信息">
    <h3>题目画面与可访问信息（只读）</h3>
    <p>让职达结合网页结构与题目画面理解控件，不点击、不滚动、不填写。请先在招聘窗口中显示要核对的题目。</p>
    <fieldset disabled={disabled||pending||!consent}>
      <label className="field"><span>观察哪道题</span><select value={selector} onChange={e=>{setSelector(e.target.value);setSample(null)}}>
        <option value="">请选择已采集题目</option>{fields.map((f,index)=><option key={f.selector} value={f.selector}>{index+1}. {f.question_text||f.label||'题干待核实'} · {f.section||f.field_type}</option>)}
      </select></label>
      <label><input type="checkbox" checked={previewImage} onChange={e=>setPreviewImage(e.target.checked)}/>本地预览时附上题目截图（不发送模型）</label>
      <button className="secondary" disabled={!selector} onClick={()=>void preview()}>只读观察这道题</button>
      <label className="observation-permission"><input type="checkbox" checked={consent?.enabled??false} onChange={e=>void authorize(e.target.checked)}/>
        允许本轮模型分析向已配置 API 服务发送遮挡后的题目截图</label>
      <small>默认不发图片。开启后模型每轮最多观察两道待分析题目；仅对当前会话、简历、资料及网址生效。题干和其他文字仍可能含个人资料，遮挡不是完全匿名化。</small>
    </fieldset>
    {!consent&&<p>观察接口尚未连接；这项升级需要后端加载新代码，不影响原有填写入口。</p>}
    {sample&&<div role="status"><p>来源：{({dom_aria:'DOM / ARIA 结构',playwright_aria:'Playwright 可访问快照',macos_ax:'macOS 原生可访问树'} as Record<string,string>)[sample.accessibility_source]||sample.accessibility_source} · 未执行填写</p>
      {sample.image_data_url&&<img src={sample.image_data_url} alt="当前题目局部截图，填写控件和照片区域已遮挡"/>}
      <ul>{sample.limitations.map((item,index)=><li key={index}>{item}</li>)}</ul>
      <details><summary>查看观察证据（不是填写答案）</summary><pre>{JSON.stringify(sample.context,null,2)}</pre><pre>{sample.accessibility}</pre></details>
    </div>}
  </section>
}
