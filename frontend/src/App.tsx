import { useEffect, useRef, useState } from 'react'
import { BriefcaseBusiness, Check, FileText, Plus, Save, Sparkles, UploadCloud } from 'lucide-react'
import { api } from './api'
import type { Education, Experience, Project, ResumeProfile, ResumeRecord } from './types'

const input = (label: string, value: string | number | null, onChange: (v: string) => void, type = 'text') => (
  <label className="field"><span>{label}</span><input type={type} value={value ?? ''} onChange={e => onChange(e.target.value)} /></label>
)

export default function App() {
  const [resumes, setResumes] = useState<ResumeRecord[]>([])
  const [selected, setSelected] = useState<string>('')
  const [draft, setDraft] = useState<ResumeRecord | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)

  useEffect(() => { api.list().then(items => { setResumes(items); if (items[0]) setSelected(items[0].id) }).catch(e => setMessage(e.message)) }, [])
  useEffect(() => { setDraft(resumes.find(r => r.id === selected) ?? null) }, [selected, resumes])

  const setProfile = (patch: Partial<ResumeProfile>) => setDraft(d => d ? ({ ...d, profile: { ...d.profile, ...patch } }) : d)
  const upload = async (file?: File) => {
    if (!file) return
    setBusy(true); setMessage('正在读取并拆分简历…')
    try { const item = await api.upload(file); setResumes(r => [item, ...r]); setSelected(item.id); setMessage('解析完成，请检查并修正字段。') }
    catch (e) { setMessage(e instanceof Error ? e.message : '上传失败') }
    finally { setBusy(false) }
  }
  const save = async () => {
    if (!draft) return
    setBusy(true)
    try { const item = await api.save(draft.id, draft.label, draft.profile); setResumes(r => r.map(x => x.id === item.id ? item : x)); setMessage('修改已保存。') }
    catch (e) { setMessage(e instanceof Error ? e.message : '保存失败') }
    finally { setBusy(false) }
  }

  return <div className="app-shell">
    <aside>
      <div className="brand"><span className="brand-icon"><BriefcaseBusiness size={20}/></span><div><strong>OfferPilot</strong><small>求职投递 Agent</small></div></div>
      <button className="upload-button" onClick={() => fileInput.current?.click()} disabled={busy}><Plus size={18}/> 上传新简历</button>
      <input ref={fileInput} hidden type="file" accept=".pdf,.docx,.txt" onChange={e => upload(e.target.files?.[0])}/>
      <div className="side-title">我的简历 <span>{resumes.length}</span></div>
      <nav>{resumes.map(r => <button key={r.id} className={selected === r.id ? 'resume-item active' : 'resume-item'} onClick={() => setSelected(r.id)}>
        <FileText size={18}/><span><strong>{r.label}</strong><small>{r.filename}</small></span>{selected === r.id && <Check size={15}/>} </button>)}</nav>
      <div className="privacy"><Sparkles size={16}/><span><strong>本地优先</strong><small>默认不把简历发送给模型</small></span></div>
    </aside>
    <main>
      <header><div><p className="eyebrow">PROFILE WORKSPACE</p><h1>把一份简历，变成可复用的求职资料</h1><p>Agent 已把简历拆成投递表单需要的字段。逐项检查一次，之后反复复用。</p></div>
        {draft && <button className="save-button" onClick={save} disabled={busy}><Save size={17}/> 保存修改</button>}
      </header>
      {message && <div className="notice">{message}</div>}
      {!draft ? <section className="empty" onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); upload(e.dataTransfer.files[0]) }}>
        <div className="upload-orbit"><UploadCloud size={30}/></div><h2>上传你的第一份简历</h2><p>拖入文件，或点击选择 PDF、DOCX、TXT（最大 10MB）</p><button onClick={() => fileInput.current?.click()}>选择简历文件</button>
      </section> : <Editor draft={draft} setDraft={setDraft} setProfile={setProfile}/>} 
    </main>
  </div>
}

function Editor({ draft, setDraft, setProfile }: { draft: ResumeRecord; setDraft: React.Dispatch<React.SetStateAction<ResumeRecord | null>>; setProfile: (p: Partial<ResumeProfile>) => void }) {
  const p = draft.profile
  const updateList = <T,>(key: 'education'|'internships'|'projects', index: number, patch: Partial<T>) => {
    const list = [...(p[key] as T[])]; list[index] = { ...list[index], ...patch }; setProfile({ [key]: list })
  }
  return <div className="editor">
    <section className="card summary-card"><div className="section-head"><div><span>01</span><div><h2>简历身份</h2><p>这份简历用于什么方向？</p></div></div><em>{draft.parser === 'local-rules' ? '规则解析' : 'AI 解析'}</em></div>
      <div className="grid two">{input('简历名称', draft.label, v => setDraft(d => d ? {...d, label: v} : d))}{input('目标岗位', p.target_role, v => setProfile({target_role:v}))}</div>
    </section>
    <section className="card"><SectionTitle number="02" title="基本信息" subtitle="投递表单中的高频字段"/><div className="grid three">
      {input('姓名', p.name, v => setProfile({name:v}))}<label className="field"><span>性别</span><select value={p.gender} onChange={e => setProfile({gender:e.target.value as ResumeProfile['gender']})}><option>未识别</option><option>男</option><option>女</option><option>其他</option></select></label>
      {input('年龄', p.age, v => setProfile({age:v ? Number(v) : null}), 'number')}{input('手机号', p.phone, v => setProfile({phone:v}))}{input('邮箱', p.email, v => setProfile({email:v}), 'email')}{input('所在地', p.location, v => setProfile({location:v}))}
    </div><label className="field full"><span>个人简介</span><textarea value={p.summary} onChange={e => setProfile({summary:e.target.value})}/></label></section>
    <ListSection<Education> number="03" title="教育经历" items={p.education} empty={{school:'',degree:'',major:'',start_date:'',end_date:''}} onItems={education => setProfile({education})} render={(item,i) => <div className="grid three">{input('学校',item.school,v=>updateList('education',i,{school:v}))}{input('学历',item.degree,v=>updateList('education',i,{degree:v}))}{input('专业',item.major,v=>updateList('education',i,{major:v}))}{input('开始时间',item.start_date,v=>updateList('education',i,{start_date:v}))}{input('结束时间',item.end_date,v=>updateList('education',i,{end_date:v}))}</div>}/>
    <ListSection<Experience> number="04" title="实习经历" items={p.internships} empty={{organization:'',role:'',start_date:'',end_date:'',description:''}} onItems={internships => setProfile({internships})} render={(item,i) => <><div className="grid two">{input('公司/组织',item.organization,v=>updateList('internships',i,{organization:v}))}{input('职位',item.role,v=>updateList('internships',i,{role:v}))}{input('开始时间',item.start_date,v=>updateList('internships',i,{start_date:v}))}{input('结束时间',item.end_date,v=>updateList('internships',i,{end_date:v}))}</div><label className="field full"><span>工作内容</span><textarea value={item.description} onChange={e=>updateList('internships',i,{description:e.target.value})}/></label></>}/>
    <ListSection<Project> number="05" title="项目经历" items={p.projects} empty={{name:'',role:'',start_date:'',end_date:'',description:'',technologies:[]}} onItems={projects => setProfile({projects})} render={(item,i) => <><div className="grid two">{input('项目名称',item.name,v=>updateList('projects',i,{name:v}))}{input('项目角色',item.role,v=>updateList('projects',i,{role:v}))}{input('开始时间',item.start_date,v=>updateList('projects',i,{start_date:v}))}{input('结束时间',item.end_date,v=>updateList('projects',i,{end_date:v}))}</div><label className="field full"><span>项目描述</span><textarea value={item.description} onChange={e=>updateList('projects',i,{description:e.target.value})}/></label></>}/>
    <section className="card"><SectionTitle number="06" title="技能关键词" subtitle="用逗号分隔，后续用于岗位匹配"/><label className="field full"><textarea value={p.skills.join('，')} onChange={e=>setProfile({skills:e.target.value.split(/[,，]/).map(x=>x.trim()).filter(Boolean)})}/></label></section>
  </div>
}

function SectionTitle({number,title,subtitle}:{number:string,title:string,subtitle:string}) { return <div className="section-head"><div><span>{number}</span><div><h2>{title}</h2><p>{subtitle}</p></div></div></div> }
function ListSection<T>({number,title,items,empty,onItems,render}:{number:string,title:string,items:T[],empty:T,onItems:(v:T[])=>void,render:(v:T,i:number)=>React.ReactNode}) { return <section className="card"><SectionTitle number={number} title={title} subtitle={`已识别 ${items.length} 条，可继续增删`}/>{items.map((item,i)=><div className="list-entry" key={i}><div className="entry-top"><strong>{title} {i+1}</strong><button onClick={()=>onItems(items.filter((_,n)=>n!==i))}>移除</button></div>{render(item,i)}</div>)}<button className="add-row" onClick={()=>onItems([...items,{...empty}])}><Plus size={16}/> 添加一条</button></section> }

