import {useEffect,useRef,useState} from 'react'
import {ArrowRight,ImagePlus,LoaderCircle,MessageSquare,Plus,Save,Send,ShieldCheck,Target,Trash2} from 'lucide-react'
import {api} from './api'
import type {ApplicationTarget,ChatConversation,ChatConversationSummary,ChatTaskDraft} from './types'
import {inspectTaskUrl,targetFromDraft,validateChatImage} from './chatTask'
import './chat.css'

const errorText=(error:unknown)=>error instanceof Error?error.message:'请求失败，请稍后重试'
const sourceText={text:'消息中的链接',qr:'二维码识别',image:'图片文字识别'}

export default function ChatWorkspace({onLaunch,onGo,activeBrowser}:{onLaunch:(target:ApplicationTarget)=>void;onGo:(page:'jobs'|'profile'|'demo')=>void;activeBrowser:boolean}) {
  const [conversations,setConversations]=useState<ChatConversationSummary[]>([])
  const [conversation,setConversation]=useState<ChatConversation|null>(null)
  const [draft,setDraft]=useState<ChatTaskDraft|null>(null)
  const [text,setText]=useState('')
  const [attachment,setAttachment]=useState<File|null>(null)
  const [preview,setPreview]=useState('')
  const [consent,setConsent]=useState(false)
  const [targetConfirmed,setTargetConfirmed]=useState(false)
  const [busy,setBusy]=useState('loading')
  const [error,setError]=useState('')
  const [localEdits,setLocalEdits]=useState(false)
  const fileInput=useRef<HTMLInputElement>(null)
  const messagesEnd=useRef<HTMLDivElement>(null)
  const requestVersion=useRef(0)
  const listSummary=(item:ChatConversation)=>{setConversations(all=>[item,...all.filter(current=>current.id!==item.id)])}
  const applyConversation=(item:ChatConversation)=>{setConversation(item);setDraft(item.draft);setTargetConfirmed(false);setLocalEdits(false)}

  useEffect(()=>{
    let current=true
    void api.chatConversations().then(async items=>{if(!current)return;setConversations(items);if(items[0]){const first=await api.chatConversation(items[0].id);if(current)applyConversation(first)}}).catch(error=>{if(current)setError(errorText(error))}).finally(()=>{if(current)setBusy('')})
    return()=>{current=false;requestVersion.current+=1}
  },[])
  useEffect(()=>{if(!attachment){setPreview('');return}const url=URL.createObjectURL(attachment);setPreview(url);return()=>URL.revokeObjectURL(url)},[attachment])
  useEffect(()=>{messagesEnd.current?.scrollIntoView({behavior:'smooth',block:'nearest'})},[conversation?.messages.length,busy])
  const ensureDraftDiscard=()=>!((text.trim()||attachment||localEdits)&&!window.confirm('切换会话将清除未发送的文字、图片和未开始的任务卡修改。已发送的历史消息会保留。是否继续？'))
  const resetComposer=()=>{setText('');setAttachment(null);setConsent(false);setTargetConfirmed(false);setError('')}
  const refreshHistory=async()=>{if(busy)return;setBusy('loading');setError('');try{setConversations(await api.chatConversations())}catch(error){setError(errorText(error))}finally{setBusy('')}}
  const openConversation=async(id:string)=>{
    if(busy||id===conversation?.id||!ensureDraftDiscard())return
    setBusy('opening');setError('');const version=++requestVersion.current
    try{const item=await api.chatConversation(id);if(version===requestVersion.current){applyConversation(item);resetComposer()}}
    catch(error){setError(errorText(error))}finally{if(version===requestVersion.current)setBusy('')}
  }
  const newConversation=async()=>{
    if(busy||!ensureDraftDiscard())return
    setBusy('creating');setError('')
    try{const item=await api.createChat();applyConversation(item);listSummary(item);resetComposer()}catch(error){setError(errorText(error))}finally{setBusy('')}
  }
  const addImage=(file?:File)=>{if(!file||busy)return;const problem=validateChatImage(file);if(problem){setError(problem);return}setAttachment(file);setConsent(false);setError('');if(fileInput.current)fileInput.current.value=''}
  const persistDraft=async()=>{
    if(!localEdits||!draft||!conversation)return conversation
    const {company,job_title,city,recruitment_cycle,url,intent}=draft
    const saved=await api.saveChatDraft(conversation.id,{company,job_title,city,recruitment_cycle,url,intent})
    applyConversation(saved);listSummary(saved)
    return saved
  }
  const saveDraft=async()=>{if(busy||!localEdits)return;setBusy('saving');setError('');try{await persistDraft()}catch(error){setError(`任务卡保存失败，修改仍保留：${errorText(error)}`)}finally{setBusy('')}}
  const send=async()=>{
    if(busy||(!text.trim()&&!attachment))return
    setBusy('sending');setError('')
    let current=conversation
    let messageStarted=false
    try{
      if(localEdits)current=await persistDraft()
      if(!current){current=await api.createChat();applyConversation(current);listSummary(current)}
      messageStarted=true
      const item=await api.sendChatMessage(current.id,text.trim(),attachment,consent)
      applyConversation(item);listSummary(item);setText('');setAttachment(null);setConsent(false)
    }catch(error){
      setError(`${errorText(error)}。${messageStarted?'未发送草稿仍保留；请检查会话是否已有回复，避免重复发送。':'任务卡尚未成功保存，文字、图片和卡片修改均保留，尚未发送消息。'}`)
      if(current&&messageStarted){try{const latest=await api.chatConversation(current.id);applyConversation(latest);listSummary(latest)}catch{/* Preserve the visible conversation and draft on refresh failure. */}}
    }finally{setBusy('')}
  }
  const editDraft=(patch:Partial<ChatTaskDraft>)=>{setDraft(current=>current?{...current,...patch}:current);setTargetConfirmed(false);setLocalEdits(true)}
  const destination=inspectTaskUrl(draft?.url||'')
  const startTask=async()=>{
    if(!draft||busy||!targetConfirmed||destination.error)return
    if(activeBrowser){setError('已有正在进行的浏览器任务。请先进入投递工作台结束旧会话，再启动新目标；不会覆盖当前网页。');return}
    const approved=targetFromDraft(draft)
    setBusy('saving');setError('')
    try{await persistDraft();onLaunch(approved)}catch(error){setError(`任务卡保存失败，尚未启动任务；修改仍保留：${errorText(error)}`)}finally{setBusy('')}
  }
  return <div className="chat-workspace">
    <aside className="chat-history"><button className="primary full" disabled={!!busy} onClick={newConversation}><Plus size={16}/>新对话</button><div className="chat-history-label">我的求职对话 <button disabled={!!busy} onClick={refreshHistory}>刷新</button></div>{conversations.map(item=><button className={item.id===conversation?.id?'selected':''} key={item.id} disabled={!!busy} onClick={()=>openConversation(item.id)}><MessageSquare size={14}/><span><strong>{item.title||'新对话'}</strong><small>{new Date(item.updated_at).toLocaleDateString()}</small></span></button>)}{!conversations.length&&!busy&&<p>发送第一条消息后，文字对话与任务草稿会保存在当前账号。</p>}</aside>
    <section className="chat-main card"><div className="chat-intro"><span><MessageSquare size={23}/></span><div><h2>说说你想投什么</h2><p>发一段岗位描述、招聘链接或截图，我们一起把它变成明确的求职任务。</p></div><b>先理解，再确认</b></div>
      <div className="chat-shortcuts"><button disabled={!!busy} onClick={()=>onGo('jobs')}><Target size={14}/>去岗位推荐</button><button disabled={!!busy} onClick={()=>onGo('profile')}>完善主档案</button>{activeBrowser&&<button onClick={()=>onGo('demo')}>回到正在进行的投递</button>}</div>
      <div className="chat-messages" aria-live="polite" role="log" aria-label="求职对话">
        {!conversation?.messages.length&&<div className="chat-empty"><h3>从一个想法开始，也可以从一张招聘海报开始。</h3><p>例如：“我想申请北京的 Agent 开发岗位，这是招聘截图，帮我看看要求和入口。”</p><small>没有招聘网址时会先补充信息，不会假装已经联网找到岗位；聊天不会自动注册、填写或提交。</small></div>}
        {conversation?.messages.map(item=><article className={'chat-message '+item.role} key={item.id}><span>{item.role==='user'?'我':'智达'}</span><div><p>{item.content}</p>{item.image_names.length>0&&<div className="chat-image-note"><ImagePlus size={13}/>{item.image_names.join('、')}<small>原图不持久保存</small></div>}<time>{new Date(item.created_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'})}</time></div></article>)}
        {busy==='sending'&&<div className="chat-processing"><LoaderCircle className="spin" size={17}/>{consent?'模型正在理解你的意图与材料':'正在本地整理文本与二维码'}，尚未操作招聘网站…</div>}
        {['loading','opening','creating'].includes(busy)&&<div className="chat-processing"><LoaderCircle className="spin" size={17}/>正在读取会话…</div>}
        <div ref={messagesEnd}/>
      </div>
      {error&&<div className="chat-error" role="alert">{error}<button type="button" onClick={()=>setError('')}>关闭</button></div>}
      <div className="chat-composer" onPaste={event=>{const image=Array.from(event.clipboardData.items).find(item=>item.kind==='file'&&item.type.startsWith('image/'))?.getAsFile();if(image){event.preventDefault();addImage(image)}}}>
        {attachment&&<div className="chat-attachment">{preview?<img src={preview} alt="即将发送的招聘截图预览"/>:<span role="status" aria-label="正在准备图片预览"><LoaderCircle className="spin" size={20}/></span>}<span><strong>{attachment.name}</strong><small>{(attachment.size/1024).toFixed(0)} KB · 仅本次发送</small></span><button disabled={!!busy} aria-label="移除图片" onClick={()=>{setAttachment(null);setConsent(false)}}><Trash2 size={16}/></button></div>}
        <textarea aria-label="求职消息" placeholder="告诉我公司、岗位或困惑，也可以粘贴招聘截图…" value={text} disabled={!!busy} maxLength={12000} onChange={event=>{setText(event.target.value);setConsent(false)}} onKeyDown={event=>{if((event.metaKey||event.ctrlKey)&&event.key==='Enter'){event.preventDefault();void send()}}}/>
        <div className="chat-composer-actions"><label className="chat-upload"><input ref={fileInput} type="file" accept="image/png,image/jpeg,image/webp" hidden disabled={!!busy} onChange={event=>addImage(event.target.files?.[0])}/><ImagePlus size={16}/>添加单张图片<small>≤ 8 MB</small></label><small>⌘ / Ctrl + Enter 发送</small><button className="primary" disabled={!!busy||(!text.trim()&&!attachment)} onClick={send}>{busy==='sending'?<LoaderCircle className="spin" size={16}/>:<Send size={16}/>} {consent?'AI 分析发送':'本地整理'}</button></div>
        <label className="chat-consent"><input type="checkbox" checked={consent} disabled={!!busy} onChange={event=>setConsent(event.target.checked)}/><span>允许将本轮文字、图片及上一版任务草稿发送到我配置的模型服务；不发送整段历史或主档案。当前账号保存文字与任务草稿，服务器原图仅用于本轮内存处理、历史仅留文件名。请勿发送密码、验证码或无关隐私。</span></label>
        {!consent&&<p className="chat-local-mode">未启用模型：只在本地整理可识别文本与二维码，不理解图片正文或复杂语义，也不会联网搜索岗位。</p>}
      </div>
    </section>
    <aside className="chat-task card"><div className="chat-task-heading"><Target size={19}/><h2>待确认任务</h2><span>{localEdits?'未保存':'已保存草稿'}</span></div>{draft?<>
      <p className="chat-task-summary">{draft.summary||'请核对下面的目标信息。'}</p>
      <div className="chat-task-fields">{([['company','公司'],['job_title','岗位 / 方向'],['city','工作地点'],['recruitment_cycle','招聘批次']] as const).map(([key,label])=><label className="field" key={key}><span>{label}</span><input value={draft[key]} disabled={!!busy} onChange={event=>editDraft({[key]:event.target.value})} placeholder="尚未确定，可补充" maxLength={key==='city'||key==='recruitment_cycle'?100:200}/></label>)}<label className="field"><span>招聘页面链接</span><textarea value={draft.url} disabled={!!busy} onChange={event=>editDraft({url:event.target.value})} placeholder="请粘贴已核对的招聘官网或 ATS 链接" maxLength={2000}/></label></div>
      <button className="secondary full" disabled={!!busy||!localEdits} onClick={saveDraft}>{busy==='saving'?<LoaderCircle className="spin" size={15}/>:<Save size={15}/>}保存任务卡</button>{localEdits&&<small className="chat-task-footnote">有未保存修改；发送下一条消息或进入工作台前会先保存，失败时不会继续。</small>}
      {draft.links.length>0&&<details className="chat-links"><summary>查看识别出的链接与来源</summary>{draft.links.map((link,index)=><div key={index}><small>{sourceText[link.source]} · 未独立验证为官网</small><code>{link.url}</code><button disabled={!!busy||!!inspectTaskUrl(link.url).error} onClick={()=>editDraft({url:link.url})}>用作任务入口</button></div>)}</details>}
      {draft.warnings.length>0&&<ul className="chat-warnings">{draft.warnings.map((warning,index)=><li key={index}>{warning}</li>)}</ul>}
      {destination.error?<p className="chat-url-warning">{destination.error}</p>:<div className="chat-destination"><small>将前往的域名（请人工核验）</small><strong>{destination.hostname}</strong><span>链接来自你的材料或模型识别，不等于已通过官网验证。</span></div>}
      <label className="chat-consent"><input type="checkbox" checked={targetConfirmed} disabled={!!busy||!!destination.error} onChange={event=>setTargetConfirmed(event.target.checked)}/><span>我已核对公司、岗位和目的域名，确认将此目标送入投递工作台；尚不授权最终提交。</span></label>
      {activeBrowser&&<div className="chat-url-warning">有任务正在进行。请先去工作台结束原会话，不会直接替换它。<button onClick={()=>onGo('demo')}>查看现有任务</button></div>}
      <button className="primary full" disabled={!!busy||!targetConfirmed||!!destination.error||activeBrowser} onClick={startTask}>确认目标，进入工作台<ArrowRight size={16}/></button><small className="chat-task-footnote">进入后点击“打开浏览器”才访问网站。任务卡会保存到此会话，但不会改主档案。</small>
      {draft.intent==='recommend'&&<button className="secondary full" onClick={()=>onGo('jobs')}>打开岗位推荐与官方源搜索</button>}{draft.intent==='profile'&&<button className="secondary full" onClick={()=>onGo('profile')}>打开主档案</button>}
    </>:<div className="chat-task-empty"><ShieldCheck size={26}/><p>聊清楚目标后，会在这里生成可编辑任务卡。</p><small>识别到的链接与二维码不会被自动打开；你始终决定下一步。</small></div>}</aside>
  </div>
}
