import { useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, ArrowRight, BriefcaseBusiness, Building2, Check, ChevronRight, CircleUserRound, Download, ExternalLink, FileJson, FileText, Files, Globe2, LayoutDashboard, ListChecks, LoaderCircle, LockKeyhole, LogOut, MapPin, MessageSquare, Play, Plus, RefreshCw, Save, ShieldCheck, Sparkles, Target, Trash2, UploadCloud, UserPlus, WandSparkles, X } from 'lucide-react'
import { API, ApiError, api } from './api'
import ApplicationKnowledge from './ApplicationKnowledge'
import ApplicationWorkspace from './ApplicationWorkspace'
import type {ApplicationWorkspaceSection} from './ApplicationWorkspace'
import {isUserAnswerQuestion,userQuestionBlockReason} from './manualQuestionPolicy'
import {checkboxQuestionGroups,checkboxGroupAnswers,checkboxGroupAnswered,checkboxGroupChecked} from './checkboxGroups'
import type {CheckboxQuestionGroup} from './checkboxGroups'
import AssistProgress from './AssistProgress'
import ControlDiagnostics from './ControlDiagnostics'
import FormExtractionQuality from './FormExtractionQuality'
import ExtractionAudit from './ExtractionAudit'
import PageObservation from './PageObservation'
import AutofillRoutingSummary from './AutofillRoutingSummary'
import ChatWorkspace from './ChatWorkspace'
import ResumeFactEditor from './ResumeFactEditor'
import {ExternalWebsiteOpening} from './ExternalWebsiteOpening'
import {ExistingSafariConnection} from './ExistingSafariConnection'
import {resumeChoiceLabel} from './resumeFacts'
import {isSelectionField,manualFillAction} from './formInputKind'
import {resumeAttachmentBlocker} from './resumeAttachment'
import {journeyBackendReady,journeyButtonLabel,journeyGuard,journeyStatus} from './applicationJourney'
import {formStageReady,inspectTaskUrl,shouldKeepChatMounted} from './chatTask'
import {inspectNavigation,navigationAction} from './workbenchNavigation'
import {canContinueWorkflow,workbenchView} from './workbenchView'
import {observedJobTitle,shouldObserveWorkbench,workflowPageChanged} from './workbenchObservation'
import './workbench-view.css'
import { actionReviewTitle, modelPending, pendingActionSelectors, routeLabel, runAutofillPhases } from './autofillRouting'
import {assistanceBackendReady,assistFailure,assistGuard,assistRequest,assistStatus,draftFieldsCompatible,forgetAssistRun,learnVerifiedDrafts,monthPrecisionMemory,needsMonthPrecisionConsent,rememberAssistRun,rememberedAssistRun,verifiedDraftEntries,watchAssist} from './applicationAssist'
import type {ApplicationAssistResult,ApplicationAssistProgress,ApplicationJourneyResult} from './types'
import type { ApplicationTarget, NavigationCandidate, ApplicationAgentTurn, ApplicationAnswerMemory, ApplicationQueueItem, ApplicationReadiness, ApplicationWorkflowState, BrowserSnapshot, CandidateProfile, CompanySize, Conflict, DiscoveryAdapter, Education, Evidence, ExecutionResult, Experience, FieldComparison, FillAction, FormPlan, JobEvidenceExplanation, JobRecommendation, JobVerification, LiveJobStatus, ModelHealth, NativeResumeImportResult, OfficialJobSource, PageField, PreSubmitCheck, Project, QueueStatus, RecommendationBatch, ResumeProfile, ResumeRecord, UserAccount } from './types'

type Page='overview'|'chat'|'jobs'|'profile'|'resumes'|'review'|'demo'
const emptyEducation:Education={school:'',college:'',degree:'',major:'',study_mode:'',academic_system:'',student_id:'',advisor:'',laboratory:'',research_direction:'',location:'',start_date:'',end_date:'',gpa:'',ranking:'',courses:[],description:'',current:false}
const emptyExperience:Experience={organization:'',department:'',role:'',employment_type:'',location:'',start_date:'',end_date:'',current:false,description:'',achievements:[],technologies:[]}
const emptyProject:Project={name:'',role:'',start_date:'',end_date:'',background:'',description:'',achievements:[],technologies:[],project_url:'',github_url:''}
const labels:Record<string,string>={name:'姓名',english_name:'英文姓名',gender:'性别',birth_date:'出生日期',age:'年龄',phone:'手机号',email:'邮箱',country_region:'国家/地区',nationality:'国籍',ethnicity:'民族',political_status:'政治面貌',marital_status:'婚姻状况',qq:'QQ',wechat:'微信',location:'当前城市',hometown:'籍贯',hukou_location:'户籍所在地',address:'通讯地址',website:'个人网站',github:'GitHub',linkedin:'LinkedIn',target_role:'目标岗位',available_date:'可入职时间',internship_duration:'实习时长',days_per_week:'每周天数',expected_salary:'期望薪资',remote_preference:'远程偏好',summary:'个人简介',education:'教育经历',internships:'实习经历',projects:'项目经历',skills:'技能'}

export default function App(){
  const [user,setUser]=useState<UserAccount|null|undefined>(undefined)
  const [page,setPage]=useState<Page>('overview'); const [profile,setProfile]=useState<CandidateProfile|null>(null)
  const [resumes,setResumes]=useState<ResumeRecord[]>([]); const [conflicts,setConflicts]=useState<Conflict[]>([])
  const [selected,setSelected]=useState(''); const [rawText,setRawText]=useState(''); const [busy,setBusy]=useState(false); const [notice,setNotice]=useState('')
  const [launchJob,setLaunchJob]=useState<{recommendation:JobRecommendation;resumeId:string;queueId:string}|null>(null)
  const [chatTarget,setChatTarget]=useState<ApplicationTarget|null>(null)
  const [chatVisitedUser,setChatVisitedUser]=useState<string|null>(null)
  const [activeBrowser,setActiveBrowser]=useState(false)
  const fileInput=useRef<HTMLInputElement>(null)
  const reload=async()=>{try{const [p,r,c]=await Promise.all([api.profile(),api.list(),api.conflicts()]);setProfile(p);setResumes(r);setConflicts(c);if(!selected&&r[0])setSelected(r[0].id)}catch(e){if(e instanceof ApiError&&e.status===401)setUser(null);else setNotice(message(e))}}
  useEffect(()=>{api.me().then(setUser).catch(()=>setUser(null))},[])
  useEffect(()=>{if(user)reload()},[user?.id])
  useEffect(()=>{if(user&&page==='chat')setChatVisitedUser(user.id)},[user?.id,page])
  useEffect(()=>{if(page!=='review'||!selected)return;let active=true;setRawText('');api.text(selected).then(data=>{if(active)setRawText(data.text)}).catch(e=>{if(active)setNotice(message(e))});return()=>{active=false}},[page,selected])
  const active=resumes.find(r=>r.id===selected)??null
  const upload=async(file?:File)=>{if(!file)return;setBusy(true);setNotice('Agent 正在读取、拆分并合并资料…');try{const item=await api.upload(file);await reload();setSelected(item.id);setPage('review');setNotice('解析完成。请检查识别证据和冲突。')}catch(e){if(e instanceof ApiError&&typeof e.detail==='object'&&e.detail&&'resume_id' in e.detail){await reload();setSelected(String((e.detail as {resume_id:unknown}).resume_id));setPage('resumes')}setNotice(message(e))}finally{setBusy(false);if(fileInput.current)fileInput.current.value=''}}
  const openReview=(id:string)=>{setSelected(id);setPage('review')}
  const updateResumeLocal=(item:ResumeRecord)=>setResumes(all=>all.map(r=>r.id===item.id?item:r))
  const nav=[['overview','总览',LayoutDashboard],['chat','求职助手',MessageSquare],['jobs','岗位推荐',Target],['profile','主档案',CircleUserRound],['resumes','简历资料库',Files],['review','待确认',AlertTriangle],['demo','投递工作台',Globe2]] as const
  const logout=async()=>{try{await api.logout()}catch{}setUser(null);setProfile(null);setResumes([]);setConflicts([]);setSelected('');setRawText('');setLaunchJob(null);setChatTarget(null);setChatVisitedUser(null);setActiveBrowser(false);setPage('overview');setNotice('')}
  if(user===undefined)return <div className="auth-loading"><LoaderCircle className="spin" size={28}/><span>正在恢复登录状态…</span></div>
  if(!user)return <AuthPage onAuthenticated={setUser}/>
  return <div className="shell"><aside><div className="brand"><span><BriefcaseBusiness size={20}/></span><div><strong>职达 Zhida</strong><small>求职资料 Agent</small></div></div>
    <button className="primary full" onClick={()=>fileInput.current?.click()} disabled={busy}><Plus size={17}/>上传新简历</button><input ref={fileInput} hidden type="file" accept=".pdf,.docx,.txt" onChange={e=>upload(e.target.files?.[0])}/>
    <nav>{nav.map(([key,label,Icon])=><button key={key} className={page===key?'active':''} onClick={()=>setPage(key)}><Icon size={18}/>{label}{key==='review'&&<b>{conflicts.length+resumes.flatMap(r=>r.evidence).filter(e=>e.status==='pending_review').length}</b>}</button>)}</nav>
    <div className="privacy"><ShieldCheck size={17}/><div><strong>受控模型传输</strong><small>解析、匹配与授权聊天时发送</small></div></div><div className={`account-chip ${user.is_local?'local':''}`}><span>{user.is_local?'本':(user.display_name||user.email)[0].toUpperCase()}</span><div><strong>{user.is_local?'本机免登录':user.display_name||'职达用户'}</strong><small>{user.is_local?'数据保存在当前电脑':user.email}</small></div>{!user.is_local&&<button title="退出登录" onClick={logout}><LogOut size={15}/></button>}</div></aside>
    <main><Top page={page}/>{notice&&<div className="notice">{notice}<button onClick={()=>setNotice('')}>×</button></div>}
      {page==='overview'&&profile&&<Overview profile={profile} resumes={resumes} conflicts={conflicts} go={setPage}/>}
      {shouldKeepChatMounted(page,chatVisitedUser,user.id)&&<div hidden={page!=='chat'}><ChatWorkspace key={user.id} activeBrowser={activeBrowser} onGo={setPage} onLaunch={target=>{if(activeBrowser){setNotice('请先进入投递工作台结束原会话，再开始新目标。');setPage('demo');return}setLaunchJob(null);setChatTarget(target);setPage('demo');setNotice('目标已带入工作台。请先在官网完成登录和选岗，再提供信息填写页网址；确认设置不会自动投递。')}}/></div>}
      {page==='jobs'&&profile&&<JobRecommendations profile={profile} resumes={resumes} notify={setNotice} onApply={(job,resumeId,queueId)=>{if(activeBrowser){setPage('demo');setNotice('已有浏览器任务正在进行，请先结束原会话。当前网页及人工修改没有被覆盖。');return}setChatTarget(null);setLaunchJob({recommendation:job,resumeId,queueId});setPage('demo');setNotice(`已选择 ${job.job.company} · ${job.job.title}，岗位链接已自动带入投递工作台。`)}}/>}
      {page==='profile'&&profile&&<ProfileEditor profile={profile} onSave={async p=>{setBusy(true);try{setProfile(await api.saveProfile(p));setNotice('主档案已保存。')}catch(e){setNotice(message(e))}finally{setBusy(false)}}}/>}
      {page==='resumes'&&<ResumeLibrary resumes={resumes} busy={busy} upload={()=>fileInput.current?.click()} onReview={openReview} onRefresh={async r=>{setBusy(true);try{updateResumeLocal(await api.reparse(r.id));await reload();setNotice('重新解析完成。')}catch(e){await reload();setNotice(message(e))}finally{setBusy(false)}}} onDefault={async r=>{updateResumeLocal(await api.saveResume(r.id,{is_default:true}));await reload()}} onDelete={async r=>{if(!confirm(`确定删除“${r.label}”及其原始文件吗？`))return;await api.deleteResume(r.id);await reload();setNotice('简历及原始文件已删除。')}}/>}
      {page==='review'&&<ReviewWorkspace resumes={resumes} active={active} conflicts={conflicts} rawText={rawText} select={openReview} notify={setNotice} onReviewed={async item=>{updateResumeLocal(item);setProfile(await api.profile())}} onResolved={async(id,choice,custom)=>{await api.resolve(id,choice,custom);await reload();setNotice('冲突已处理，主档案已更新。')}}/>}
      <div hidden={page!=='demo'}><BrowserDemo key={user.id} ownerId={user.id} active={page==='demo'} profile={profile} resumes={resumes} notify={setNotice} onProfileUpdate={setProfile} onResumeUpdate={updateResumeLocal} initialJob={launchJob?.recommendation??null} initialResumeId={launchJob?.resumeId??''} initialQueueId={launchJob?.queueId??''} initialTarget={chatTarget} onSessionChange={setActiveBrowser}/></div>
    </main></div>
}

function AuthPage({onAuthenticated}:{onAuthenticated:(user:UserAccount)=>void}){
  const [mode,setMode]=useState<'login'|'register'>('login');const [displayName,setDisplayName]=useState('');const [email,setEmail]=useState('');const [password,setPassword]=useState('');const [confirmPassword,setConfirmPassword]=useState('');const [busy,setBusy]=useState(false);const [error,setError]=useState('')
  const switchMode=(next:'login'|'register')=>{setMode(next);setError('');setPassword('');setConfirmPassword('')}
  const submit=async(e:React.FormEvent)=>{e.preventDefault();setError('');if(mode==='register'&&password!==confirmPassword){setError('两次输入的密码不一致');return}setBusy(true);try{const session=mode==='register'?await api.register(email,password,displayName):await api.login(email,password);onAuthenticated(session.user)}catch(err){setError(message(err))}finally{setBusy(false)}}
  return <main className="auth-shell"><section className="auth-brand"><span><BriefcaseBusiness size={27}/></span><strong>职达 Zhida</strong><small>让每一份求职资料都有清晰归属</small><div><ShieldCheck size={18}/><p><b>账号级资料隔离</b><br/>密码只保存安全哈希，简历和投递清单按用户分开。</p></div></section><section className="auth-card"><div className="auth-tabs"><button className={mode==='login'?'active':''} onClick={()=>switchMode('login')}>登录</button><button className={mode==='register'?'active':''} onClick={()=>switchMode('register')}>创建账号</button></div><div className="auth-heading"><span>{mode==='login'?<LockKeyhole size={19}/>:<UserPlus size={19}/>}</span><div><h1>{mode==='login'?'欢迎回来':'创建你的职达账号'}</h1><p>{mode==='login'?'继续管理简历和投递任务':'第一位注册用户会自动接管当前本地资料'}</p></div></div><form onSubmit={submit}>{mode==='register'&&<label><span>显示名称</span><input autoComplete="name" value={displayName} onChange={e=>setDisplayName(e.target.value)} placeholder="例如：李春博"/></label>}<label><span>邮箱账号</span><input type="email" autoComplete="email" required value={email} onChange={e=>setEmail(e.target.value)} placeholder="name@example.com"/></label><label><span>密码</span><input type="password" autoComplete={mode==='login'?'current-password':'new-password'} required minLength={8} value={password} onChange={e=>setPassword(e.target.value)} placeholder="至少 8 位，包含两类字符"/></label>{mode==='register'&&<label><span>确认密码</span><input type="password" autoComplete="new-password" required minLength={8} value={confirmPassword} onChange={e=>setConfirmPassword(e.target.value)} placeholder="再次输入密码"/></label>}{error&&<div className="auth-error"><AlertTriangle size={15}/>{error}</div>}<button className="primary auth-submit" disabled={busy}>{busy?<LoaderCircle className="spin" size={17}/>:mode==='login'?<LockKeyhole size={17}/>:<UserPlus size={17}/>} {busy?'请稍候':mode==='login'?'登录':'创建账号并进入'}</button></form><footer>本地开发版使用 HttpOnly 会话 Cookie；部署时请开启 HTTPS。</footer></section></main>
}

function Top({page}:{page:Page}){const copy={chat:['求职助手','通过文字与招聘截图明确目标，再进入可控的投递流程。'],overview:['候选人资料总览','一次维护，反复用于之后的每次申请。'],jobs:['推荐投递清单','根据已确认简历生成可解释推荐，选中后直接进入投递流程。'],profile:['候选人主档案','这里是你的事实来源，Agent 不会擅自覆盖已确认信息。'],resumes:['简历资料库','管理不同语言、岗位方向和版本的简历。'],review:['识别结果检查','对照原文确认 Agent 拆分的字段，并处理资料冲突。'],demo:['投递工作台','选择简历和信息填写页，剩下的交给职达辅助。']}[page];return <header><div><p className="eyebrow">ZHIDA / PROFILE WORKSPACE</p><h1>{copy[0]}</h1><p>{copy[1]}</p></div>{page!=='demo'&&<a className="export" href={`${API}/export`} target="_blank" rel="noreferrer"><FileJson size={17}/>导出 JSON</a>}</header>}

function Overview({profile,resumes,conflicts,go}:{profile:CandidateProfile;resumes:ResumeRecord[];conflicts:Conflict[];go:(p:Page)=>void}){
  const pending=resumes.flatMap(r=>r.evidence).filter(e=>e.status==='pending_review').length
  const fields=[profile.name,profile.phone,profile.email,profile.location,profile.target_role,profile.summary,profile.education.length,profile.internships.length,profile.projects.length,profile.skills.length]
  const complete=Math.round(fields.filter(Boolean).length/fields.length*100)
  return <><section className="metric-grid"><Metric label="资料完整度" value={`${complete}%`} note="继续补充可提升投递覆盖" tone="green"/><Metric label="简历版本" value={String(resumes.length)} note={resumes.some(r=>r.is_default)?'已设置默认版本':'还没有默认简历'}/><Metric label="待确认字段" value={String(pending)} note="确认后成为可信资料"/><Metric label="信息冲突" value={String(conflicts.length)} note={conflicts.length?'需要你做最终选择':'目前没有冲突'} tone={conflicts.length?'amber':'green'}/></section>
    <section className="overview-grid"><div className="card profile-hero"><div className="avatar">{profile.name?.[0]||'职'}</div><div><p>候选人主档案</p><h2>{profile.name||'尚未填写姓名'}</h2><span>{profile.target_role||'添加目标岗位'} · {profile.location||'添加当前城市'}</span></div><button onClick={()=>go('profile')}>编辑档案<ChevronRight size={16}/></button></div>
    <div className="card next-step"><Sparkles size={22}/><div><h3>下一步建议</h3><p>{pending?`还有 ${pending} 个识别字段等待确认。`:resumes.length?'主档案已经可以用于生成推荐投递清单。':'上传一份简历，Agent 会自动拆分并生成推荐。'}</p></div><button onClick={()=>go(pending?'review':resumes.length?'jobs':'resumes')}>{!pending&&resumes.length?'查看推荐':'开始处理'}</button></div></section>
    <section className="card"><SectionTitle n="资料" title="经历素材概览" sub="主档案中的可复用事实"/><div className="fact-row"><Fact n={profile.education.length} label="教育经历"/><Fact n={profile.internships.length} label="实习经历"/><Fact n={profile.projects.length} label="项目经历"/><Fact n={profile.skills.length} label="技能关键词"/></div></section></>}
function Metric({label,value,note,tone=''}:{label:string;value:string;note:string;tone?:string}){return <div className={`metric ${tone}`}><span>{label}</span><strong>{value}</strong><small>{note}</small></div>}
function Fact({n,label}:{n:number;label:string}){return <div><strong>{n}</strong><span>{label}</span></div>}

function JobRecommendations({profile,resumes,notify,onApply}:{profile:CandidateProfile;resumes:ResumeRecord[];notify:(v:string)=>void;onApply:(job:JobRecommendation,resumeId:string,queueId:string)=>void}){
  const [batch,setBatch]=useState<RecommendationBatch|null>(null);const [queue,setQueue]=useState<ApplicationQueueItem[]>([]);const [sources,setSources]=useState<OfficialJobSource[]>([]);const [readiness,setReadiness]=useState<ApplicationReadiness|null>(null);const [modelHealth,setModelHealth]=useState<ModelHealth|null>(null);const [explanations,setExplanations]=useState<Record<string,JobEvidenceExplanation>>({});const [selectedEvidenceJob,setSelectedEvidenceJob]=useState('');const [selectedJobs,setSelectedJobs]=useState<Set<string>>(new Set());const [company,setCompany]=useState('全部公司');const [companySize,setCompanySize]=useState<CompanySize|''>('');const [location,setLocation]=useState('');const [keyword,setKeyword]=useState(profile.target_role||'');const [visibleLimit,setVisibleLimit]=useState(20);const [selectedResume,setSelectedResume]=useState('');const [busy,setBusy]=useState('');const [showSourceForm,setShowSourceForm]=useState(false);const [sourceCompany,setSourceCompany]=useState('');const [sourceUrl,setSourceUrl]=useState('');const [sourceSize,setSourceSize]=useState<CompanySize>('unknown');const [sourceAdapter,setSourceAdapter]=useState<DiscoveryAdapter>('auto');const [sourceKey,setSourceKey]=useState('');const [outcomePrompt,setOutcomePrompt]=useState<ApplicationQueueItem|null>(null);const [dismissedOutcomes,setDismissedOutcomes]=useState<Set<string>>(new Set())
  const ragRequest=(nextLocation=location,nextKeyword='')=>({location:nextLocation,query:nextKeyword,company_sizes:companySize?[companySize]:[]})
  const load=async(nextLocation=location)=>{setBusy('loading');try{const [nextBatch,nextQueue,nextSources,nextReadiness]=await Promise.all([api.ragRecommendations(ragRequest(nextLocation)),api.jobQueue(),api.jobSources(),api.readiness()]);setBatch(nextBatch);setQueue(nextQueue);setSources(nextSources);setReadiness(nextReadiness);setExplanations({})}catch(e){notify(message(e))}finally{setBusy('')}}
  useEffect(()=>{load()},[profile.updated_at])
  useEffect(()=>{if(!selectedResume){const preferred=resumes.find(item=>item.is_default)??resumes[0];if(preferred)setSelectedResume(preferred.id)}},[resumes,selectedResume])
  useEffect(()=>{if(!outcomePrompt){const pending=queue.find(item=>item.confirmation_pending&&!dismissedOutcomes.has(item.id));if(pending)setOutcomePrompt(pending)}},[queue,outcomePrompt,dismissedOutcomes])
  const companies=useMemo(()=>['全部公司',...Array.from(new Set(batch?.jobs.map(item=>item.job.company)??[]))],[batch])
  const filtered=useMemo(()=>{const query=keyword.trim().toLocaleLowerCase();return batch?.jobs.filter(item=>(company==='全部公司'||item.job.company===company)&&(!companySize||item.job.company_size===companySize)&&(!query||[item.job.company,item.job.title,item.job.job_code,...item.job.required_skills,...item.job.role_keywords].join(' ').toLocaleLowerCase().includes(query)))??[]},[batch,company,companySize,keyword])
  const visible=useMemo(()=>filtered.slice(0,visibleLimit),[filtered,visibleLimit])
  const evidenceItem=visible.find(item=>item.job.id===selectedEvidenceJob)??visible[0]
  const queuedIds=useMemo(()=>new Set(queue.map(item=>item.job_id)),[queue])
  const profileGap=!profile.target_role&&!profile.skills.length?'目标岗位和技能':!profile.target_role?'目标岗位':'技能'
  const toggle=(id:string)=>setSelectedJobs(current=>{const next=new Set(current);if(next.has(id))next.delete(id);else next.add(id);return next})
  const changeLocation=(value:string)=>{setLocation(value);setCompany('全部公司');setVisibleLimit(20);setSelectedJobs(new Set());void load(value)}
  const changeCompany=(value:string)=>{setCompany(value);setVisibleLimit(20);setSelectedJobs(new Set())}
  const changeKeyword=(value:string)=>{setKeyword(value);setVisibleLimit(20);setSelectedJobs(new Set())}
  const smartSearch=async(sync=true)=>{setBusy('syncing');try{const result=await api.smartJobSearch({query:keyword,location,company_sizes:companySize?[companySize]:[],sync_sources:sync,max_sources:12});const [ragBatch,nextQueue,nextSources,nextReadiness]=await Promise.all([api.ragRecommendations(ragRequest(location,keyword)),api.jobQueue(),api.jobSources(),api.readiness()]);setBatch(ragBatch);setQueue(nextQueue);setSources(nextSources);setReadiness(nextReadiness);setExplanations({});setCompany('全部公司');setVisibleLimit(20);setSelectedJobs(new Set());notify(`智能搜岗完成：${result.successful_sources}/${result.synced_sources} 个官方源可用，读取 ${result.discovered_jobs} 条，匹配出 ${ragBatch.jobs.length} 个岗位；${ragBatch.rag_message}${result.failed_sources?`；${result.failed_sources} 个来源暂时失败，已保留缓存`:''}。`)}catch(e){notify(message(e))}finally{setBusy('')}}
  const syncSources=()=>smartSearch(true)
  const addSource=async()=>{if(!sourceCompany.trim()||!sourceUrl.trim()){notify('请填写公司名称和官方招聘网址。');return}setBusy('adding-source');try{const source=await api.addJobSource({company:sourceCompany,official_url:sourceUrl,adapter:sourceAdapter,source_key:sourceKey,company_size:sourceSize});setSources(await api.jobSources());setSourceCompany('');setSourceUrl('');setSourceKey('');setSourceAdapter('auto');setSourceSize('unknown');setShowSourceForm(false);notify(`已识别并添加 ${source.company} 的 ${source.adapter} 招聘源，可以开始同步。`)}catch(e){notify(message(e))}finally{setBusy('')}}
  const deleteSource=async(source:OfficialJobSource)=>{if(!source.user_added)return;if(!window.confirm(`删除“${source.name}”及其本地岗位缓存？`))return;setBusy(`source:${source.id}`);try{await api.deleteJobSource(source.id);setSources(await api.jobSources());await load(location);notify('自定义招聘源已删除。')}catch(e){notify(message(e))}finally{setBusy('')}}
  const checkModel=async()=>{setBusy('model-health');try{const result=await api.modelHealth();setModelHealth(result);notify(`${result.model}：${result.message}（${result.latency_ms}ms）`)}catch(e){notify(message(e))}finally{setBusy('')}}
  const add=async(ids:string[])=>{if(!ids.length)return;setBusy('queue');try{setQueue(await api.queueJobs(ids,selectedResume));setSelectedJobs(new Set());notify(`已将 ${ids.length} 个岗位加入候选清单；开始投递前会重新核验。`)}catch(e){notify(message(e))}finally{setBusy('')}}
  const verify=async(item:JobRecommendation):Promise<JobVerification|null>=>{setBusy(`verify:${item.job.id}`);try{const result=await api.verifyJob(item.job.id);setBatch(await api.ragRecommendations(ragRequest(location)));notify(`${item.job.company} · ${item.job.title}：${result.message}`);return result}catch(e){notify(message(e));return null}finally{setBusy('')}}
  const begin=async(item:JobRecommendation,resumeId=selectedResume)=>{setBusy(item.job.id);try{const verification=await api.verifyJob(item.job.id);const refreshed=await api.ragRecommendations(ragRequest(location));setBatch(refreshed);const current=refreshed.jobs.find(candidate=>candidate.job.id===item.job.id)??item;if(['closed','mismatch','unreachable'].includes(verification.status)){notify(`已停止：${verification.message}`);return}const tracked=await api.queueJobs([item.job.id],resumeId);const record=tracked.find(entry=>entry.job_id===item.job.id);if(record&&['submitted','interview','offer'].includes(record.status)){setQueue(tracked);notify(`已阻止重复投递：该岗位当前状态为“${queueStatusLabel(record.status)}”。如确实需要重新申请，请先在流水账中调整状态。`);return}if(record){const updated=await api.updateQueuedJob(record.id,{status:'in_progress'});setQueue(tracked.map(entry=>entry.id===updated.id?updated:entry))}else setQueue(tracked);if(!current.formal_queue_eligible)notify(verification.status==='manual_gate'?'静态官网核验证据不足，即将先在 Chrome 中只读核验，不会直接填写或提交。':`官网可以访问，但${current.gate_reasons.join('、')}；本次进入人工核实流程。`);onApply(current,resumeId,record?.id??'')}catch(e){notify(message(e))}finally{setBusy('')}}
  const explain=async(item:JobRecommendation)=>{setBusy(`explain:${item.job.id}`);try{const detail=await api.explainJob(item.job.id);setExplanations(current=>({...current,[item.job.id]:detail}));if(detail.status==='local_fallback')notify('模型代理暂不可用，已展示带原文依据的本地解释。')}catch(e){notify(message(e))}finally{setBusy('')}}
  const changeQueueStatus=async(item:ApplicationQueueItem,status:QueueStatus)=>{const confirmed=['submitted','interview','offer'].includes(status);if(confirmed&&!window.confirm(`请确认“${item.recommendation.job.company} · ${item.recommendation.job.title}”已经真实进入“${queueStatusLabel(status)}”状态。系统不会替你点击最终提交。`))return;setBusy(item.id);try{const updated=await api.updateQueuedJob(item.id,{status,candidate_confirmed:confirmed});setQueue(current=>current.map(entry=>entry.id===updated.id?updated:entry));setReadiness(await api.readiness());notify(`投递状态已更新为“${queueStatusLabel(status)}”。`)}catch(e){notify(message(e))}finally{setBusy('')}}
  const saveQueueText=async(item:ApplicationQueueItem,field:'notes'|'application_id',value:string)=>{if(value===item[field])return;setBusy(item.id);try{const updated=await api.updateQueuedJob(item.id,{[field]:value});setQueue(current=>current.map(entry=>entry.id===updated.id?updated:entry));notify(field==='notes'?'投递备注已保存。':'申请编号已保存。')}catch(e){notify(message(e))}finally{setBusy('')}}
  const remove=async(id:string)=>{setBusy(id);try{await api.removeQueuedJob(id);setQueue(current=>current.filter(item=>item.id!==id));notify('已从投递清单移除。')}catch(e){notify(message(e))}finally{setBusy('')}}
  const confirmOutcome=async(submitted:boolean)=>{if(!outcomePrompt)return;setBusy(`outcome:${outcomePrompt.id}`);try{const updated=await api.updateQueuedJob(outcomePrompt.id,{status:submitted?'submitted':'needs_review',candidate_confirmed:true});setQueue(current=>current.map(item=>item.id===updated.id?updated:item));setOutcomePrompt(null);setReadiness(await api.readiness());notify(submitted?'已由你确认真实投递，并写入投递时间。':'已记录为尚未投递，保留在待复核清单中。')}catch(e){notify(message(e))}finally{setBusy('')}}
  const postponeOutcome=()=>{if(!outcomePrompt)return;setDismissedOutcomes(current=>new Set(current).add(outcomePrompt.id));setOutcomePrompt(null)}
  const today=new Date().toLocaleDateString('en-CA');const submittedToday=queue.filter(item=>item.submitted_at&&new Date(item.submitted_at).toLocaleDateString('en-CA')===today).length;const activeToday=queue.filter(item=>['in_progress','needs_review','ready_to_submit'].includes(item.status)).length
  if(!batch&&busy==='loading')return <section className="card recommendation-loading"><LoaderCircle className="spin" size={24}/><span>正在根据主档案计算岗位匹配度…</span></section>
  return <div className="stack jobs-workspace"><section className="card recommendation-hero"><div><span className="demo-icon"><Target size={22}/></span><div><h2>简历驱动的智能搜岗</h2><p>{batch?.profile_summary||'等待读取主档案'} · 官方招聘源实时同步、标准化检索与可解释匹配</p></div></div><div className="recommendation-stats"><strong>{filtered.length}</strong><span>个当前匹配岗位</span><small>{batch?.engine}</small></div></section>
    {readiness&&<section className={`card readiness-card ${readiness.ready?'ready':'blocked'}`}><div className="readiness-score"><ShieldCheck size={22}/><strong>{readiness.score}</strong><span>投递准备度</span></div><div className="readiness-body"><h3>{readiness.ready?'基础资料可以开始投递':'开始批量投递前仍有阻塞项'}</h3><p>{readiness.blockers.length?readiness.blockers.join('；'):'姓名、联系方式、教育经历和简历文件已就绪。'}</p>{readiness.warnings.length>0&&<small>建议处理：{readiness.warnings.join('；')}</small>}{modelHealth&&<small className={`model-health ${modelHealth.status}`}>模型 {modelHealth.model}：{modelHealth.message} · {modelHealth.latency_ms}ms</small>}</div><div className="readiness-stats"><span><strong>{activeToday}</strong>处理中</span><span><strong>{submittedToday}</strong>今日已投</span><span><strong>{readiness.official_sources_ready}/{readiness.official_sources_total}</strong>岗位源</span><button className="secondary" disabled={!!busy} onClick={checkModel}>{busy==='model-health'?<LoaderCircle className="spin" size={13}/>:<RefreshCw size={13}/>}检查模型</button></div></section>}
    {(!profile.target_role||!profile.skills.length)&&<div className="profile-warning"><AlertTriangle size={17}/><span><strong>推荐依据还不完整</strong><small>补充{profileGap}后，匹配结果会更准确。</small></span></div>}
    <section className="card"><div className="recommendation-toolbar"><div><SectionTitle n="01" title="联网智能搜岗" sub="从不同规模公司的官方招聘源读取当前职位，再依据简历、地点和目标方向匹配"/></div><div className="recommendation-filters"><label><span>目标岗位</span><input aria-label="搜索岗位" value={keyword} onChange={e=>changeKeyword(e.target.value)} placeholder="Agent / 大模型 / 后端"/></label><label><span>工作地点</span><select aria-label="工作地点" value={location} disabled={!!busy} onChange={e=>changeLocation(e.target.value)}><option value="">全部地点（按主档案排序）</option>{batch?.available_locations.map(item=><option value={item} key={item}>{item}</option>)}</select></label><label><span>公司阶段</span><select aria-label="公司阶段" value={companySize} onChange={e=>{setCompanySize(e.target.value as CompanySize|'');setVisibleLimit(20)}}><option value="">全部规模</option><option value="large">大型公司</option><option value="growth">成长公司</option><option value="startup">创业公司</option><option value="unknown">未标注</option></select></label><label><span>公司</span><select aria-label="公司" value={company} onChange={e=>changeCompany(e.target.value)}>{companies.map(item=><option key={item}>{item}</option>)}</select></label><button className="primary smart-search-button" disabled={!!busy||!sources.length} onClick={syncSources}><Globe2 size={15}/>{busy==='syncing'?'正在搜索官方岗位':'联网智能搜岗'}</button><button className="secondary" disabled={!!busy} onClick={()=>smartSearch(false)}><RefreshCw className={busy==='loading'?'spin':''} size={15}/>使用本地缓存匹配</button><button className="secondary" disabled={!!busy} onClick={()=>setShowSourceForm(value=>!value)}><Plus size={15}/>添加招聘源</button></div></div>
      {showSourceForm&&<div className="source-add-panel"><div><strong>添加公司官方招聘源</strong><small>粘贴公司 Greenhouse、Lever、Ashby、SmartRecruiters 或带 JobPosting 结构化数据的招聘官网；系统会自动识别。</small></div><label><span>公司名称</span><input value={sourceCompany} onChange={e=>setSourceCompany(e.target.value)} placeholder="例如：某 AI 创业公司"/></label><label className="source-url"><span>官方招聘网址</span><input value={sourceUrl} onChange={e=>setSourceUrl(e.target.value)} placeholder="https://jobs.ashbyhq.com/company"/></label><label><span>公司阶段</span><select value={sourceSize} onChange={e=>setSourceSize(e.target.value as CompanySize)}><option value="large">大型公司</option><option value="growth">成长公司</option><option value="startup">创业公司</option><option value="unknown">不确定</option></select></label><label><span>适配器</span><select value={sourceAdapter} onChange={e=>setSourceAdapter(e.target.value as DiscoveryAdapter)}><option value="auto">自动识别</option><option value="greenhouse">Greenhouse</option><option value="lever">Lever</option><option value="ashby">Ashby</option><option value="smartrecruiters">SmartRecruiters</option><option value="jsonld">通用 JobPosting</option></select></label>{sourceAdapter!=='auto'&&sourceAdapter!=='jsonld'&&<label><span>Source key（可选）</span><input value={sourceKey} onChange={e=>setSourceKey(e.target.value)} placeholder="通常可从网址自动识别"/></label>}<button className="primary" disabled={busy==='adding-source'} onClick={addSource}>{busy==='adding-source'?<LoaderCircle className="spin" size={14}/>:<Plus size={14}/>}保存来源</button></div>}
      {batch&&<div className={`rag-status ${batch.rag_status}`}><Sparkles size={16}/><span><strong>{batch.rag_status==='ready'?'本地向量 + 关键词混合检索':batch.rag_status==='keyword_only'?'仅关键词检索':batch.rag_status==='no_evidence'?'缺少经历证据':'传统岗位匹配'}</strong><small>{batch.rag_message}{batch.rag_evidence_count>0&&` · ${batch.rag_evidence_count} 条经历证据 · ${batch.rag_enriched_jobs} 个岗位已核对`}</small></span></div>}
      <div className="source-sync-strip">{sources.map(source=><div className={source.last_status} key={source.id}><Globe2 size={14}/><span><strong>{source.name}<b className={`company-size ${source.company_size}`}>{companySizeLabel(source.company_size)}</b></strong><small>{source.adapter} · {source.last_message}{source.last_completed_at&&` · ${formatCheckedAt(source.last_completed_at)}`}</small></span><em>{source.last_status==='never'?'未同步':source.last_status==='failed'?'同步失败':source.partial?`${source.jobs_seen}/${source.total_available??'?'} 条（部分）`:`${source.jobs_seen} 条`}</em>{source.user_added&&<button className="source-delete" title="删除自定义来源" disabled={!!busy} onClick={()=>deleteSource(source)}><Trash2 size={13}/></button>}</div>)}</div>
      <div className="batch-bar"><label><input type="checkbox" checked={Boolean(visible.length)&&visible.every(item=>selectedJobs.has(item.job.id))} onChange={e=>setSelectedJobs(e.target.checked?new Set(visible.map(item=>item.job.id)):new Set())}/>选择当前显示</label><span>显示 {visible.length}/{filtered.length} · 已选 {selectedJobs.size} 个</span><label className="queue-resume"><span>投递简历</span><select value={selectedResume} onChange={e=>setSelectedResume(e.target.value)}><option value="">暂不指定</option>{resumes.map(item=><option value={item.id} key={item.id}>{resumeChoiceLabel(item)}{item.is_default?' · 默认':''}</option>)}</select></label><button className="primary" disabled={!selectedJobs.size||!!busy} onClick={()=>add([...selectedJobs])}><ListChecks size={16}/>加入候选清单</button></div>
      <div className="job-grid">{visible.map(item=><article className={`job-card ${selectedJobs.has(item.job.id)?'selected':''}`} key={item.job.id}><div className="job-card-top"><label className="job-check"><input aria-label={`选择 ${item.job.company} ${item.job.title}`} type="checkbox" checked={selectedJobs.has(item.job.id)} onChange={()=>toggle(item.job.id)}/></label><div className="company-mark"><Building2 size={20}/></div><div className="job-title"><span>{item.job.company} · {companySizeLabel(item.job.company_size)} · {item.job.recruitment_type}</span><h3>{item.job.title}</h3><p><MapPin size={12}/>{item.job.locations.join(' / ')||'地点以官网为准'}{item.job.job_code&&` · ${item.job.job_code}`}</p></div><div className={`score ${item.match_score>=75?'high':item.match_score>=60?'medium':'low'}`}><strong>{item.match_score}</strong><span>匹配分</span></div></div><p className="job-description">{item.job.description}</p>{item.job.discovery_source&&<div className="discovery-meta"><Globe2 size={13}/><span>{item.job.discovery_scope}{item.job.source_updated_at&&` · 官网更新 ${item.job.source_updated_at}`}</span></div>}<div className="match-columns"><div><span>已命中</span><div className="skill-tags matched">{item.matched_skills.length?item.matched_skills.map(skill=><b key={skill}>{skill}</b>):<small>暂未识别明确技能</small>}</div></div><div><span>待核实</span><div className="skill-tags missing">{item.missing_skills.length?item.missing_skills.map(skill=><b key={skill}>{skill}</b>):<small>没有硬技能缺口</small>}</div></div></div><ul className="reason-list">{item.reasons.slice(0,3).map(reason=><li key={reason}>{reason}</li>)}</ul>{item.job.verification_message&&<div className={`verification-note ${item.job.live_status}`}><strong>{liveStatusLabel(item.job.live_status)}</strong><span>{item.job.verification_message}</span>{item.job.verification_evidence.length>0&&<small>{item.job.verification_evidence.join(' · ')}</small>}</div>}{!item.formal_queue_eligible&&<div className="gate-note"><AlertTriangle size={14}/><span>进入正式队列前：{item.gate_reasons.join('；')}</span></div>}<footer><div><span className={`source-status ${item.job.live_status}`}>{liveStatusLabel(item.job.live_status)}{item.job.last_checked_at&&` · ${formatCheckedAt(item.job.last_checked_at)}`}</span><em title={item.gate_reasons.join('、')}>{item.formal_queue_eligible?'正式队列可用':item.queue_track==='stretch'?'挑战岗位':'稳妥岗位'}</em></div><div><a className="secondary" href={item.job.source_url} target="_blank" rel="noreferrer">来源<ExternalLink size={13}/></a><button className="secondary" disabled={!!busy} onClick={()=>verify(item)}>{busy===`verify:${item.job.id}`?<LoaderCircle className="spin" size={13}/>:<RefreshCw size={13}/>}官网核验</button><button className="secondary" disabled={queuedIds.has(item.job.id)||!!busy} onClick={()=>add([item.job.id])}>{queuedIds.has(item.job.id)?'已在清单':'加入清单'}</button><button className="primary" disabled={['closed','mismatch','unreachable'].includes(item.job.live_status)||!!busy} onClick={()=>begin(item)}>{busy===item.job.id?<LoaderCircle className="spin" size={15}/>:<Play size={15}/>} {['closed','mismatch','unreachable'].includes(item.job.live_status)?'暂不可投':item.job.live_status==='manual_gate'?'浏览器核验':item.job.apply_mode==='direct'?'开始投递':'打开选岗入口'}</button></div></footer></article>)}{!visible.length&&<div className="empty job-empty"><MapPin size={28}/><h2>当前筛选下没有岗位</h2><p>可以更换工作地点、公司阶段或目标岗位后再试。</p></div>}</div>
      {visible.length<filtered.length&&<button className="load-more secondary" onClick={()=>setVisibleLimit(limit=>limit+20)}>再显示 20 个岗位（剩余 {filtered.length-visible.length}）</button>}
      {evidenceItem&&<RecommendationEvidence item={evidenceItem} jobs={visible} selected={evidenceItem.job.id} onSelect={setSelectedEvidenceJob} explanation={explanations[evidenceItem.job.id]} loading={busy===`explain:${evidenceItem.job.id}`} onExplain={()=>explain(evidenceItem)}/>}
    </section>
    <section className="card"><div className="library-head"><SectionTitle n={String(queue.length).padStart(2,'0')} title="投递流水账" sub="跟踪每个岗位，避免重复投递；只有你确认后才能标记为已投"/><a className="secondary" href={`${API}/jobs/queue-export.csv`}><Download size={14}/>导出 CSV</a></div>{queue.length?<div className="queue-list">{queue.map(item=><article className={`queue-${item.status}`} key={item.id}><div className="queue-company">{item.recommendation.job.company.slice(0,1)}</div><div className="queue-main"><strong>{item.recommendation.job.title}</strong><span>{item.recommendation.job.company} · {item.recommendation.match_score}% 匹配 · {item.recommendation.formal_queue_eligible?'正式校招门槛已通过':'仍有门槛待核实'}</span>{item.assistance_started_at&&<small>AI 辅助开始：{new Date(item.assistance_started_at).toLocaleString('zh-CN')}{item.confirmation_pending?' · 等待确认投递结果':''}</small>}{item.submitted_at&&<small>确认投递：{new Date(item.submitted_at).toLocaleString('zh-CN')}</small>}</div><div className="queue-controls"><select aria-label={`${item.recommendation.job.title}投递状态`} value={item.status} disabled={busy===item.id} onChange={e=>changeQueueStatus(item,e.target.value as QueueStatus)}>{queueStatuses.map(status=><option value={status} key={status}>{queueStatusLabel(status)}</option>)}</select><button className="secondary" disabled={!!busy||['submitted','interview','offer'].includes(item.status)} onClick={()=>begin(item.recommendation,item.resume_id)}><Play size={14}/>{item.status==='planned'?'开始投递':'继续处理'}</button><button className="queue-remove" title={['submitted','interview','offer'].includes(item.status)?'已产生正式记录，请先调整状态后再移除':'从流水账移除'} disabled={!!busy||['submitted','interview','offer'].includes(item.status)} onClick={()=>remove(item.id)}><Trash2 size={14}/></button></div><div className="queue-details"><label><span>申请编号</span><input key={`${item.id}-${item.application_id}`} defaultValue={item.application_id} placeholder="提交后可记录" onBlur={e=>saveQueueText(item,'application_id',e.target.value)}/></label><label><span>备注</span><input key={`${item.id}-${item.notes}`} defaultValue={item.notes} placeholder="缺失字段、截止时间、跟进信息…" onBlur={e=>saveQueueText(item,'notes',e.target.value)}/></label></div></article>)}</div>:<div className="empty queue-empty"><ListChecks size={28}/><h2>投递流水账还是空的</h2><p>勾选上方岗位加入清单，开始投递后会自动进入“填写中”。</p></div>}</section>
    {outcomePrompt&&<div className="outcome-backdrop" role="dialog" aria-modal="true" aria-label="确认投递结果"><div className="outcome-dialog"><span className="outcome-icon"><ListChecks size={22}/></span><div><small>投递结果确认</small><h2>刚才的岗位已经完成最终投递了吗？</h2><p><strong>{outcomePrompt.recommendation.job.company} · {outcomePrompt.recommendation.job.title}</strong></p><p>职达只辅助到最终提交按钮之前，无法也不会代替你确认提交。请告诉我们真实结果，系统才能避免重复投递。</p></div><div className="outcome-actions"><button className="primary" disabled={!!busy} onClick={()=>confirmOutcome(true)}><Check size={15}/>是，已经投递</button><button className="secondary" disabled={!!busy} onClick={()=>confirmOutcome(false)}>还没有，保留待复核</button><button className="text-button" disabled={!!busy} onClick={postponeOutcome}>稍后再问</button></div></div></div>}
  </div>
}

function RecommendationEvidence({item,jobs,selected,onSelect,explanation,loading,onExplain}:{item:JobRecommendation;jobs:JobRecommendation[];selected:string;onSelect:(id:string)=>void;explanation?:JobEvidenceExplanation;loading:boolean;onExplain:()=>void}){
  const direct=item.evidence_matches.filter(match=>match.support==='direct')
  const related=item.evidence_matches.filter(match=>match.support==='related')
  return <section className="card evidence-workspace"><div className="library-head"><SectionTitle n="证据" title="为什么推荐这个岗位" sub="逐条岗位要求对应主档案原文；语义相近但未证实的内容不会算作已命中"/><select aria-label="查看岗位推荐依据" value={selected} onChange={e=>onSelect(e.target.value)}>{jobs.map(job=><option key={job.job.id} value={job.job.id}>{job.job.company} · {job.job.title}</option>)}</select></div><div className="evidence-grid"><div><h3>直接依据</h3>{direct.length?direct.map(match=><article key={`${match.requirement}-${match.evidence_id}`}><strong>{match.requirement}</strong><span>{match.source_kind==='project'?'项目':'实习'} · {match.source_title}</span><p>{match.quote}</p><small>主档案位置：{match.source_path}</small></article>):<p className="evidence-empty">暂未找到能直接证明岗位要求的项目或实习事实。</p>}</div><div><h3>相关但未证实 / 缺口</h3>{related.map(match=><article key={`${match.requirement}-${match.evidence_id}`}><strong>{match.requirement} · 需核对</strong><span>{match.source_title}</span><p>{match.quote}</p></article>)}{item.evidence_gaps.length?<ul>{item.evidence_gaps.map(gap=><li key={gap}>{gap}</li>)}</ul>:<p className="evidence-empty">已列出的硬技能要求都有直接证据；其他资格仍以官网为准。</p>}</div></div><div className="evidence-explain-action"><button className="secondary" disabled={loading||!direct.length} onClick={onExplain}>{loading?<LoaderCircle className="spin" size={14}/>:<Sparkles size={14}/>}让模型解释这些依据</button><small>点击后，仅将上面列出的岗位要求与经历片段发送给当前模型代理；模型不可用时显示本地证据解释。</small></div>{explanation&&<div className="evidence-explanation"><strong>{explanation.status==='model'?`模型解读 · ${explanation.model}`:explanation.status==='no_evidence'?'证据不足':'本地证据解释'}</strong><p>{explanation.summary}</p><ul>{explanation.supported_reasons.map((reason,index)=><li key={index}>{reason}</li>)}</ul>{explanation.gaps.length>0&&<small>尚未证实：{explanation.gaps.join('、')}</small>}</div>}</section>
}

const queueStatuses:QueueStatus[]=['planned','in_progress','needs_review','ready_to_submit','submitted','interview','offer','rejected','withdrawn']
const queueStatusLabel=(status:QueueStatus)=>({planned:'计划中',in_progress:'填写中',needs_review:'待复核',ready_to_submit:'待提交',submitted:'已投递',interview:'面试中',offer:'Offer',rejected:'未通过',withdrawn:'已撤回'}[status])
const companySizeLabel=(size:CompanySize)=>({large:'大型公司',growth:'成长公司',startup:'创业公司',unknown:'规模待确认'}[size])
const liveStatusLabel=(status:LiveJobStatus)=>({not_checked:'等待实时核验',open:'官网实时可投',closed:'官网显示已关闭',manual_gate:'需浏览器核验',mismatch:'岗位信息不匹配',unreachable:'本次核验失败'}[status])
const formatCheckedAt=(value:string)=>{const date=new Date(value);return Number.isNaN(date.getTime())?value:date.toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'})}

function ProfileEditor({profile,onSave}:{profile:CandidateProfile;onSave:(p:ResumeProfile)=>void}){
  const [draft,setDraft]=useState<ResumeProfile>(profile);useEffect(()=>setDraft(profile),[profile])
  const set=(key:keyof ResumeProfile,value:unknown)=>setDraft(d=>({...d,[key]:value}))
  return <div className="stack"><section className="card"><SectionTitle n="01" title="基本信息" sub="敏感信息只由你填写或确认"/><div className="form-grid three">
    <Input label="姓名" value={draft.name} set={v=>set('name',v)}/><Input label="英文姓名" value={draft.english_name} set={v=>set('english_name',v)}/><Select label="性别" value={draft.gender} values={['未识别','男','女','其他']} set={v=>set('gender',v)}/><Input label="出生日期" type="date" value={draft.birth_date} set={v=>set('birth_date',v)}/><Input label="年龄" type="number" value={draft.age} set={v=>set('age',v?Number(v):null)}/><Input label="手机号" value={draft.phone} set={v=>set('phone',v)}/><Input label="邮箱" type="email" value={draft.email} set={v=>set('email',v)}/><Input label="国家/地区" value={draft.country_region} set={v=>set('country_region',v)}/><Input label="国籍" value={draft.nationality} set={v=>set('nationality',v)}/><Input label="民族" value={draft.ethnicity} set={v=>set('ethnicity',v)}/><Input label="政治面貌" value={draft.political_status} set={v=>set('political_status',v)}/><Input label="婚姻状况" value={draft.marital_status} set={v=>set('marital_status',v)}/><Input label="QQ" value={draft.qq} set={v=>set('qq',v)}/><Input label="微信" value={draft.wechat} set={v=>set('wechat',v)}/><Input label="当前城市" value={draft.location} set={v=>set('location',v)}/><Input label="籍贯" value={draft.hometown} set={v=>set('hometown',v)}/><Input label="户籍所在地" value={draft.hukou_location} set={v=>set('hukou_location',v)}/><Input label="通讯地址" value={draft.address} set={v=>set('address',v)}/><Input label="GitHub" value={draft.github} set={v=>set('github',v)}/><Input label="LinkedIn" value={draft.linkedin} set={v=>set('linkedin',v)}/><Input label="个人网站" value={draft.website} set={v=>set('website',v)}/></div><Text label="个人简介" value={draft.summary} set={v=>set('summary',v)}/></section>
    <section className="card"><SectionTitle n="补充" title="常用网申资料" sub="只保存本人确认的事实；籍贯、户口、生源地、现居住地分别使用。退役状态仍需在当次表单确认；无正式工作不代表无实习。"/><div className="form-grid three">
      <Input label="身高（cm）" type="number" value={draft.height_cm??''} set={v=>set('height_cm',v?Number(v):null)}/>
      <Input label="体重（kg）" type="number" value={draft.weight_kg??''} set={v=>set('weight_kg',v?Number(v):null)}/>
      <Input label="生源地（高考所在地，省/市/区）" value={draft.student_origin||''} set={v=>set('student_origin',v)}/>
      <Select label="是否为退役军人" value={draft.veteran_status||''} values={['','是','否']} set={v=>set('veteran_status',v)}/>
      <Select label="高中到最高学历学习时间是否连续" value={draft.study_continuity||''} values={['','是','否']} set={v=>set('study_continuity',v)}/>
      <Select label="正式劳动合同及社保工作经历（不含实习）" value={draft.formal_employment_status||''} values={['','有','无']} set={v=>set('formal_employment_status',v)}/>
    </div></section>
    <section className="card"><SectionTitle n="02" title="求职偏好" sub="后续用于职位筛选和申请表单"/><div className="form-grid three"><Input label="目标岗位" value={draft.target_role} set={v=>set('target_role',v)}/><Input label="目标城市（逗号分隔）" value={draft.target_cities.join('，')} set={v=>set('target_cities',split(v))}/><Input label="目标行业（逗号分隔）" value={draft.target_industries.join('，')} set={v=>set('target_industries',split(v))}/><Input label="意向事业群（逗号分隔）" value={draft.preferred_business_groups.join('，')} set={v=>set('preferred_business_groups',split(v))}/><Input label="面试方式/城市偏好（逗号分隔）" value={draft.interview_preferences.join('，')} set={v=>set('interview_preferences',split(v))}/><Input label="是否接受调剂/异地分配" value={draft.willing_to_relocate} set={v=>set('willing_to_relocate',v)}/><Input label="校招候选人类型" value={draft.campus_candidate_type} set={v=>set('campus_candidate_type',v)}/><Input label="可入职时间" value={draft.available_date} set={v=>set('available_date',v)}/><Input label="实习时长" value={draft.internship_duration} set={v=>set('internship_duration',v)}/><Input label="每周可实习天数" value={draft.days_per_week} set={v=>set('days_per_week',v)}/><Input label="期望薪资" value={draft.expected_salary} set={v=>set('expected_salary',v)}/><Input label="远程偏好" value={draft.remote_preference} set={v=>set('remote_preference',v)}/></div></section>
    <EditableList title="教育经历" n="03" items={draft.education} empty={emptyEducation} set={v=>set('education',v)} render={(x,i,edit)=><div className="form-grid three"><Input label="学校" value={x.school} set={v=>edit(i,{school:v})}/><Input label="学院" value={x.college} set={v=>edit(i,{college:v})}/><Input label="专业" value={x.major} set={v=>edit(i,{major:v})}/><Input label="学历" value={x.degree} set={v=>edit(i,{degree:v})}/><Input label="培养/学习方式" value={x.study_mode} set={v=>edit(i,{study_mode:v})}/><Input label="学制" value={x.academic_system} set={v=>edit(i,{academic_system:v})}/><Input label="学号" value={x.student_id} set={v=>edit(i,{student_id:v})}/><Input label="导师" value={x.advisor} set={v=>edit(i,{advisor:v})}/><Input label="实验室" value={x.laboratory} set={v=>edit(i,{laboratory:v})}/><Input label="研究方向" value={x.research_direction} set={v=>edit(i,{research_direction:v})}/><Input label="就读地点" value={x.location} set={v=>edit(i,{location:v})}/><Input label="开始时间" value={x.start_date} set={v=>edit(i,{start_date:v})}/><Input label="毕业时间" value={x.end_date} set={v=>edit(i,{end_date:v})}/><Input label="GPA" value={x.gpa} set={v=>edit(i,{gpa:v})}/><Input label="排名" value={x.ranking} set={v=>edit(i,{ranking:v})}/></div>}/>
    <EditableList title="实习经历" n="04" items={draft.internships} empty={emptyExperience} set={v=>set('internships',v)} render={(x,i,edit)=><><div className="form-grid three"><Input label="公司/组织" value={x.organization} set={v=>edit(i,{organization:v})}/><Input label="部门" value={x.department} set={v=>edit(i,{department:v})}/><Input label="职位" value={x.role} set={v=>edit(i,{role:v})}/><Input label="地点" value={x.location} set={v=>edit(i,{location:v})}/><Input label="开始时间" value={x.start_date} set={v=>edit(i,{start_date:v})}/><Input label="结束时间" value={x.end_date} set={v=>edit(i,{end_date:v})}/></div><Text label="工作内容" value={x.description} set={v=>edit(i,{description:v})}/><Text label="实习成果（每行一条）" value={x.achievements.join('\n')} set={v=>edit(i,{achievements:v.split(/\r?\n/).map(s=>s.trim()).filter(Boolean)})}/><Input label="使用技术（逗号分隔）" value={x.technologies.join('，')} set={v=>edit(i,{technologies:split(v)})}/></>}/>
    <EditableList title="项目经历" n="05" items={draft.projects} empty={emptyProject} set={v=>set('projects',v)} render={(x,i,edit)=><><div className="form-grid three"><Input label="项目名称" value={x.name} set={v=>edit(i,{name:v})}/><Input label="角色" value={x.role} set={v=>edit(i,{role:v})}/><Input label="开始时间" value={x.start_date} set={v=>edit(i,{start_date:v})}/><Input label="结束时间" value={x.end_date} set={v=>edit(i,{end_date:v})}/><Input label="技术栈（逗号分隔）" value={x.technologies.join('，')} set={v=>edit(i,{technologies:split(v)})}/><Input label="GitHub" value={x.github_url} set={v=>edit(i,{github_url:v})}/></div><Text label="项目描述" value={x.description} set={v=>edit(i,{description:v})}/><Text label="项目中职责（本人实际承担的工作）" value={x.responsibilities||''} set={v=>edit(i,{responsibilities:v})}/><Text label="项目成果（每行一条）" value={x.achievements.join('\n')} set={v=>edit(i,{achievements:v.split(/\r?\n/).map(s=>s.trim()).filter(Boolean)})}/></>}/>
    <section className="card"><SectionTitle n="06" title="技能与补充资料" sub="使用逗号分隔多个项目"/><div className="form-grid two"><Text label="技能" value={draft.skills.join('，')} set={v=>set('skills',split(v))}/><Text label="语言能力" value={draft.languages.join('，')} set={v=>set('languages',split(v))}/><Text label="证书" value={draft.certificates.join('，')} set={v=>set('certificates',split(v))}/><Text label="奖项" value={draft.awards.join('，')} set={v=>set('awards',split(v))}/></div></section>
    <SavedAnswersEditor answers={draft.application_answers} set={value=>set('application_answers',value)} memories={draft.application_answer_memory||[]} setMemories={value=>set('application_answer_memory',value)}/>
    <div className="sticky-save"><span>主档案是 Agent 后续工作的事实基础</span><button className="primary" onClick={()=>onSave(draft)}><Save size={17}/>保存主档案</button></div></div>}

function SavedAnswersEditor({answers,set,memories,setMemories}:{answers:Record<string,string>;set:(answers:Record<string,string>)=>void;memories:ApplicationAnswerMemory[];setMemories:(items:ApplicationAnswerMemory[])=>void}){const entries=Object.entries(answers);const change=(oldQuestion:string,question:string,value:string)=>{const next={...answers};delete next[oldQuestion];if(question.trim())next[question.trim()]=value;set(next)};return <section className="card"><SectionTitle n="07" title="已学习的申请答案" sub="只记录你明确确认过的非敏感答案；字段身份和网页选项一致时才会复用"/>{entries.length?<div className="saved-answer-list">{entries.map(([question,value])=><div key={question}><input aria-label="问题" value={question} onChange={e=>change(question,e.target.value,value)}/><input aria-label="答案" value={value} onChange={e=>change(question,question,e.target.value)}/><button className="danger" onClick={()=>{const next={...answers};delete next[question];set(next)}}>删除</button></div>)}</div>:<p className="saved-answer-empty">还没有跨网站常用答案。你在投递工作台确认安全问题后，系统会自动学习。</p>}{memories.length>0&&<div className="memory-list"><div className="memory-head"><strong>精确字段记忆</strong><span>{memories.length} 条</span></div>{memories.map(item=><article key={item.id}><div><strong>{item.question}</strong><span>{item.value}</span><small>{item.source_host||'本地表单'} · {item.entity_scope} · 已确认 {item.confirmed_count} 次</small></div><button className="danger" onClick={()=>setMemories(memories.filter(memory=>memory.id!==item.id))}>删除记忆</button></article>)}</div>}</section>}

function ResumeLibrary({resumes,busy,upload,onReview,onRefresh,onDefault,onDelete}:{resumes:ResumeRecord[];busy:boolean;upload:()=>void;onReview:(id:string)=>void;onRefresh:(r:ResumeRecord)=>void;onDefault:(r:ResumeRecord)=>void;onDelete:(r:ResumeRecord)=>void}){return <section className="card"><div className="library-head"><SectionTitle n={String(resumes.length).padStart(2,'0')} title="简历版本" sub="支持 PDF、DOCX、TXT，单份最大 10MB"/><button className="primary" onClick={upload}><UploadCloud size={17}/>上传简历</button></div>{!resumes.length?<div className="empty"><UploadCloud size={30}/><h2>还没有简历</h2><p>上传后，Agent 会自动拆分并合并到主档案。</p></div>:<div className="resume-grid">{resumes.map(r=><article className="resume-card" key={r.id}><div className="file-icon"><Files size={22}/></div><div className="resume-main"><div><h3>{r.label}{r.is_default&&<em>默认</em>}</h3><p>{r.filename}</p></div><div className="tag-row"><span>{r.language}</span><span>{formatBytes(r.file_size)}</span><span>{r.status==='failed'?'解析失败':r.status==='parsing'?'解析中':r.parser==='local-rules'?'本地解析':'AI 解析'}</span></div><small>{r.status==='failed'?(r.error_message||'模型服务暂时不可用，请重新解析'):`${r.evidence.filter(e=>e.status==='pending_review').length} 个字段待确认 · ${new Date(r.updated_at).toLocaleDateString('zh-CN')}`}</small></div><div className="actions"><button disabled={r.status==='failed'||r.status==='parsing'} onClick={()=>onReview(r.id)}>检查</button><a href={`${API}/resumes/${r.id}/download`}><Download size={15}/></a><button title="重新解析" disabled={busy||r.status==='parsing'} onClick={()=>onRefresh(r)}><RefreshCw size={15}/></button>{!r.is_default&&<button title="设为默认" disabled={r.status==='failed'||r.status==='parsing'} onClick={()=>onDefault(r)}><Check size={15}/></button>}<button className="danger" title="删除" onClick={()=>onDelete(r)}><Trash2 size={15}/></button></div></article>)}</div>}</section>}

function ReviewWorkspace({resumes,active,conflicts,rawText,select,notify,onReviewed,onResolved}:{resumes:ResumeRecord[];active:ResumeRecord|null;conflicts:Conflict[];rawText:string;select:(id:string)=>void;notify:(v:string)=>void;onReviewed:(r:ResumeRecord)=>Promise<void>;onResolved:(id:string,c:'current'|'incoming'|'custom',v?:unknown)=>void}){
  const [tab,setTab]=useState<'fields'|'conflicts'>('fields')
  const [sourceView,setSourceView]=useState<'file'|'text'>('file')
  useEffect(()=>setSourceView('file'),[active?.id])
  const pendingEvidence=active?.evidence.filter(e=>e.status==='pending_review')??[]
  return <div className="stack"><div className="review-toolbar"><select value={active?.id||''} onChange={e=>select(e.target.value)}><option value="">选择简历</option>{resumes.map(r=><option value={r.id} key={r.id}>{resumeChoiceLabel(r)}</option>)}</select><div><button className={tab==='fields'?'active':''} onClick={()=>setTab('fields')}>识别字段</button><button className={tab==='conflicts'?'active':''} onClick={()=>setTab('conflicts')}>资料冲突 <b>{conflicts.length}</b></button></div></div>
    {tab==='conflicts'?<ConflictList conflicts={conflicts} resolve={onResolved}/>:!active?<div className="card empty"><Files size={28}/><h2>选择一份简历开始检查</h2></div>:<div className="review-grid"><section className="card source"><div className="source-head"><div><h2>{sourceView==='file'?'原始文件预览':'提取文本'}</h2><p>{active.filename}</p></div><div className="source-head-tools"><div className="source-switch"><button className={sourceView==='file'?'active':''} onClick={()=>setSourceView('file')}><Files size={13}/>原始文件</button><button className={sourceView==='text'?'active':''} onClick={()=>setSourceView('text')}><FileText size={13}/>提取文本</button></div><a href={`${API}/resumes/${active.id}/download`} title="下载原始文件"><Download size={15}/></a></div></div>{sourceView==='file'?<iframe className="source-frame" title={`${active.filename} 原始文件预览`} src={`${API}/resumes/${active.id}/preview`}/>:<pre>{rawText||'正在读取提取文本…'}</pre>}</section><section className="card evidence"><div className="source-head"><div><h2>Agent 识别结果</h2><p>处理后会自动从待确认列表收起</p></div><span>{pendingEvidence.length} 待处理</span></div>{pendingEvidence.length?pendingEvidence.map(e=><EvidenceItem key={e.id} item={e} notify={notify} act={async(status,value)=>onReviewed(await api.review(active.id,e.id,status,value))}/>):<div className="success-empty"><ShieldCheck size={30}/><h3>这份简历已检查完成</h3><p>所有字段均已确认、修改或拒绝。</p></div>}</section></div>}</div>}
function EvidenceItem({item,act,notify}:{item:Evidence;act:(s:string,v?:unknown)=>Promise<void>;notify:(v:string)=>void}){const [editing,setEditing]=useState(false);const [value,setValue]=useState(typeof item.value==='string'?item.value:JSON.stringify(item.value,undefined,2));const tone=item.confidence>=.9?'high':item.confidence>=.75?'medium':'low';const submit=async(status:string,next?:unknown)=>{try{await act(status,next);if(status==='edited')notify(`${labels[item.field_path]??item.field_path}已同步到简历版本和主档案。`);return true}catch(e){notify(message(e));return false}};const save=async()=>{let parsed:unknown=value;if(typeof item.value!=='string'){try{parsed=JSON.parse(value)}catch{notify('列表或经历字段必须是有效的 JSON，请检查括号和引号。');return}}if(await submit('edited',parsed))setEditing(false)};return <article className={`evidence-item ${item.status}`}><div className="evidence-top"><strong>{labels[item.field_path]??item.field_path}</strong><span className={tone}>{Math.round(item.confidence*100)}% 置信度</span></div>{editing?<textarea value={value} onChange={e=>setValue(e.target.value)}/>:<div className="evidence-value">{value}</div>}<blockquote>{item.source_text||'未找到直接原文'}</blockquote><div className="evidence-actions"><span>等待确认</span>{editing?<><button onClick={save}>保存修改</button><button onClick={()=>setEditing(false)}>取消</button></>:<><button onClick={()=>submit('confirmed')}><Check size={14}/>确认</button><button onClick={()=>setEditing(true)}>修改</button><button className="reject" onClick={()=>submit('rejected')}>拒绝</button></>}</div></article>}
function ConflictList({conflicts,resolve}:{conflicts:Conflict[];resolve:(id:string,c:'current'|'incoming'|'custom',v?:unknown)=>void}){return <section className="card"><SectionTitle n={String(conflicts.length).padStart(2,'0')} title="资料冲突" sub="新简历不会静默覆盖主档案，请选择真实信息"/>{!conflicts.length?<div className="success-empty"><ShieldCheck size={30}/><h3>没有待处理冲突</h3><p>主档案中的信息目前一致。</p></div>:<div className="conflict-list">{conflicts.map(c=><article key={c.id}><div><span>{labels[c.field_path]??c.field_path}</span><small>来自：{c.resume_label}</small></div><button onClick={()=>resolve(c.id,'current')}><small>保留主档案</small><strong>{display(c.current_value)}</strong></button><button onClick={()=>resolve(c.id,'incoming')}><small>采用新简历</small><strong>{display(c.incoming_value)}</strong></button></article>)}</div>}</section>}

function BrowserDemo({ownerId,active,profile,resumes,notify,onProfileUpdate,onResumeUpdate,initialJob,initialResumeId,initialQueueId,initialTarget,onSessionChange}:{ownerId:string;active:boolean;initialTarget:ApplicationTarget|null;onSessionChange:(active:boolean)=>void;profile:CandidateProfile|null;resumes:ResumeRecord[];notify:(v:string)=>void;onProfileUpdate:(profile:CandidateProfile)=>void;onResumeUpdate:(resume:ResumeRecord)=>void;initialJob:JobRecommendation|null;initialResumeId:string;initialQueueId:string}){
  const defaultResumeId=initialResumeId||(resumes.find(item=>item.is_default)??resumes[0])?.id||''
  const initialResume=resumes.find(item=>item.id===defaultResumeId)
  const [url,setUrl]=useState(initialTarget?.source_url||initialJob?.job.url||'');const [snapshot,setSnapshot]=useState<BrowserSnapshot|null>(null);const [workflow,setWorkflow]=useState<ApplicationWorkflowState|null>(null);const [agentTurn,setAgentTurn]=useState<ApplicationAgentTurn|null>(null);const [verificationCode,setVerificationCode]=useState('');const [registrationEmail,setRegistrationEmail]=useState(profile?.email||'');const [registrationPhone,setRegistrationPhone]=useState(profile?.phone||'');const [registrationPassword,setRegistrationPassword]=useState('');const [plan,setPlan]=useState<FormPlan|null>(null);const [comparisons,setComparisons]=useState<FieldComparison[]>([]);const [nativeImport,setNativeImport]=useState<NativeResumeImportResult|null>(null);const [showMatched,setShowMatched]=useState(false);const [execution,setExecution]=useState<ExecutionResult|null>(null);const [check,setCheck]=useState<PreSubmitCheck|null>(null);const [answers,setAnswers]=useState<Record<string,string>>({});const [skippedSelectors,setSkippedSelectors]=useState<Set<string>>(new Set());const [editingSelectors,setEditingSelectors]=useState<Set<string>>(new Set());const [selectedResume,setSelectedResume]=useState(defaultResumeId);const [busy,setBusyState]=useState('')
  const agentRevision=useRef(0)
  const restoreRequest=useRef(0)
  const [openedSafari,setOpenedSafari]=useState<{url:string;token:string}|null>(null)
  const hasMatchingSafari=Boolean(openedSafari&&openedSafari.url===inspectTaskUrl(url).url)
  const [browserOccupied,setBrowserOccupied]=useState(false)
  const [backendReady,setBackendReady]=useState(false)
  const [recordCompletionReady,setRecordCompletionReady]=useState(false)
  const [journeyReady,setJourneyReady]=useState(false)
  const [journeyResult,setJourneyResult]=useState<ApplicationJourneyResult|null>(null)
  const [memoryFailures,setMemoryFailures]=useState<string[]>([])
  const [checkedSkips,setCheckedSkips]=useState<{selectors:string[];snapshot:BrowserSnapshot;resumeId:string}|null>(null)
  const [assistResult,setAssistResult]=useState<ApplicationAssistResult|null>(null)
  const [assistProgress,setAssistProgress]=useState<ApplicationAssistProgress|null>(null)
  const [controlDiagnostics,setControlDiagnostics]=useState<Record<string,unknown>|null>(null)
  const assistRun=useRef<{session:string;id:string;active:boolean}|null>(null)
  useEffect(()=>()=>{if(assistRun.current)assistRun.current.active=false},[])
  const [allowSiteParse,setAllowSiteParse]=useState(false)
  const [workspaceStage,setWorkspaceStage]=useState<'setup'|'workspace'>('setup')
  const [workspaceSection,setWorkspaceSection]=useState<ApplicationWorkspaceSection>('operation')
  const changeTaskResume=async(id:string)=>{
    if(busy||id===selectedResume)return
    if(!snapshot){setSelectedResume(id);resetReview();return}
    if(!backendReady){notify('本轮自动补齐升级尚未加载，请先保留招聘草稿，再重启后端。');return}
    if(snapshot&&(Object.values(answers).some(v=>v.trim())||skippedSelectors.size)){notify('请先填写并验证或清除本页临时答案，再切换简历。');return}
    if(snapshot&&!window.confirm('切换后会清空旧分析计划并重新核对。招聘网页上已填写的内容和已上传的附件不会自动撤销，请重新检查。'))return
    setBusy('resume-switching')
    try{if(snapshot)await api.selectTaskResume(snapshot.session_id,id);setSelectedResume(id);resetReview();setAgentTurn(null);setNativeImport(null);notify('已切换本次填写依据。')}
    catch(e){notify(message(e))}finally{setBusy('')}
  }
  const [recoveryWarning,setRecoveryWarning]=useState('')
  const [taskTarget,setTaskTarget]=useState<ApplicationTarget|null>(initialTarget)
  const [observedChange,setObservedChange]=useState<ApplicationWorkflowState|null>(null)
  const stalePage=Boolean(observedChange)
  const skipsChecked=Boolean(snapshot&&checkedSkips&&checkedSkips.resumeId===selectedResume&&checkedSkips.selectors.length===skippedSelectors.size&&checkedSkips.selectors.every(selector=>skippedSelectors.has(selector))&&checkedSkips.snapshot.fields.length===snapshot.fields.length&&draftFieldsCompatible(checkedSkips.snapshot.fields.map(field=>field.selector),checkedSkips.snapshot,snapshot))
  useEffect(()=>{if(checkedSkips&&(!skipsChecked||stalePage))setCheckedSkips(null)},[checkedSkips,skipsChecked,stalePage])
  const observationState=useRef({workflow,snapshot,busy,active})
  observationState.current={workflow,snapshot,busy,active}
  useEffect(()=>{
    if(!active||!snapshot?.session_id)return
    let disposed=false,inFlight=false,lastStarted=0
    const observe=async()=>{
      const latest=observationState.current
      const sessionId=latest.snapshot?.session_id||''
      if(!shouldObserveWorkbench({active:latest.active,visible:document.visibilityState==='visible',busy:Boolean(latest.busy),sessionId,hasWorkflow:Boolean(latest.workflow)})||inFlight||Date.now()-lastStarted<1500)return
      inFlight=true;lastStarted=Date.now()
      const revision=agentRevision.current
      try{
        // This endpoint only observes page metadata; never call snapshot/model here.
        const observed=await api.workflowState(sessionId)
        const current=observationState.current
        if(disposed||!current.active||document.visibilityState!=='visible'||current.busy||current.snapshot?.session_id!==sessionId||revision!==agentRevision.current)return
        if(workflowPageChanged(current.workflow,observed)){agentRevision.current+=1;setObservedChange(observed)}
        // Once detected, keep old plans locked until the user explicitly synchronizes.
      }catch{/* A passive observation error never destroys a task or starts recovery actions. */}
      finally{inFlight=false}
    }
    window.addEventListener('focus',observe)
    document.addEventListener('visibilitychange',observe)
    void observe()
    return()=>{disposed=true;window.removeEventListener('focus',observe);document.removeEventListener('visibilitychange',observe)}
  },[active,snapshot?.session_id])
  useEffect(()=>{onSessionChange(Boolean(snapshot)||browserOccupied)},[snapshot?.session_id,browserOccupied,onSessionChange])
  const restoreExistingSession=async()=>{
    const revision=++restoreRequest.current;setBusy('restoring');setRecoveryWarning('')
    try{
      const remembered=rememberedAssistRun(window.sessionStorage,ownerId,window.location.hash)
      if(remembered){
        const run={...remembered,active:true};assistRun.current=run;setBusy('assisting')
        rememberAssistRun(window.sessionStorage,ownerId,remembered)
        // Restoring progress is not restoring a write task. Only receipt GETs
        // run while the original task may still hold the browser operation lock.
        const result=await watchAssist(()=>api.assistProgress(run.session,run.id),p=>{
          if(run.active&&revision===restoreRequest.current)setAssistProgress(p)
        },()=>run.active&&revision===restoreRequest.current)
        run.active=false
        if(revision!==restoreRequest.current)return
        forgetAssistRun(window.sessionStorage,ownerId)
        const params=new URLSearchParams(window.location.hash.replace(/^#/,''))
        if(params.has('assist-session')||params.has('assist-run'))window.history.replaceState(null,'',window.location.pathname+window.location.search)
        if(result){
          setAssistResult(result);setSnapshot(result.snapshot);setUrl(result.snapshot.url)
          setPlan(result.review?.plan??null);setComparisons(result.review?.comparisons??[]);setCheck(result.pre_submit)
          const current=await api.currentBrowser()
          if(revision!==restoreRequest.current)return
          setBackendReady(assistanceBackendReady(current));setRecordCompletionReady((current.record_completion_version??0)>=1);setJourneyReady(journeyBackendReady(current));setBrowserOccupied(current.occupied);setSelectedResume(current.resume_id||'')
          const state=await api.workflowState(run.session)
          if(revision!==restoreRequest.current)return
          setWorkflow(state);setTaskTarget(state.target||null);setObservedChange(state)
          setRecoveryWarning('已只读恢复上轮进度和结果，没有重发填写。结果来自上轮核验；继续操作前请同步当前页。')
          return
        }
      }
      const current=await api.currentBrowser()
      if(revision!==restoreRequest.current)return
      setBackendReady(assistanceBackendReady(current));setRecordCompletionReady((current.record_completion_version??0)>=1);setJourneyReady(journeyBackendReady(current));setBrowserOccupied(current.occupied)
      if(current.session_id){
        const [savedSnapshot,savedWorkflow]=await Promise.all([api.browserSnapshot(current.session_id),api.workflowState(current.session_id)])
        if(revision!==restoreRequest.current)return
        const draftSelectors=[...Object.keys(answers),...skippedSelectors]
        if(snapshot&&draftSelectors.length&&(current.resume_id!==selectedResume||!draftFieldsCompatible(draftSelectors,snapshot,savedSnapshot))){
          setObservedChange(savedWorkflow);setRecoveryWarning('已只读查看新页面，但未执行答案与当前页面或简历不兼容。答案仍保留，旧计划保持暂停；请先核对并清除不适用的临时修改，再同步。');return
        }
        // A read-only refresh must never reactivate an old plan against a new DOM.
        setPlan(null);setComparisons([]);setCheck(null);setExecution(null);setAssistResult(null);setJourneyResult(null);setAgentTurn(null)
        setSnapshot(savedSnapshot);setWorkflow(savedWorkflow);setUrl(savedSnapshot.url);setTaskTarget(savedWorkflow.target||null);setSelectedResume(current.resume_id||'');setObservedChange(null)
        if(draftSelectors.length){
          const fresh=await api.reviewForm(current.session_id)
          if(draftFieldsCompatible(draftSelectors,savedSnapshot,fresh.snapshot)){setSnapshot(fresh.snapshot);setPlan(fresh.plan);setComparisons(fresh.comparisons)}
          else{setObservedChange(savedWorkflow);setRecoveryWarning('同步过程中题目发生变化，答案仍保留；旧计划已停用，请先核对临时修改。')}
        }
      }else if(current.occupied){setRecoveryWarning('浏览器正被另一账号的任务占用，不能覆盖。请等待原任务结束，或切回所属账号结束会话。')}
      else if(snapshot){
        // A restarted backend has no live browser. Do not keep a stale form
        // looking usable; retain target/resume and any unexecuted draft answers.
        agentRevision.current+=1
        setSnapshot(null);setWorkflow(null);setObservedChange(null);setPlan(null);setComparisons([])
        setCheck(null);setExecution(null);setAssistResult(null);setJourneyResult(null);setAgentTurn(null)
        setRecoveryWarning('原招聘浏览器会话已结束（可能是后端重启）。目标岗位、简历选择和未执行答案仍保留；请重新打开招聘页面，不会执行旧填写计划。')
      }
    }catch(error){if(revision!==restoreRequest.current)return;if(assistRun.current)assistRun.current.active=false;if(error instanceof ApiError&&[401,403,404].includes(error.status))forgetAssistRun(window.sessionStorage,ownerId);setBackendReady(false);setJourneyReady(false);setBrowserOccupied(true);setRecoveryWarning(`暂时无法确认现有浏览器任务状态，已暂停创建新任务以免覆盖。${message(error)}`)}finally{if(revision===restoreRequest.current)setBusy('')}
  }
  useEffect(()=>{void restoreExistingSession();return()=>{restoreRequest.current+=1}},[])
  useEffect(()=>{if(initialTarget&&!snapshot){setTaskTarget(initialTarget);setUrl(initialTarget.source_url)}},[initialTarget])
  const [phaseProgress,setPhaseProgress]=useState<{rules:number|null;model:number|null}>({rules:null,model:null})
  const setBusy=(value:string)=>{if(value)agentRevision.current+=1;setBusyState(value)}
  useEffect(()=>()=>{agentRevision.current+=1},[])
  const assessInBackground=(sessionId:string)=>{const revision=agentRevision.current;void api.assessApplicationAgent(sessionId).then(turn=>{if(revision===agentRevision.current)setAgentTurn(turn)}).catch(()=>{if(revision===agentRevision.current)notify('自动评估未完成，请点击“重新评估”。')})}
  useEffect(()=>{if(initialJob?.job.url&&!snapshot){setUrl(initialJob.job.url);setTaskTarget({company:initialJob.job.company,job_title:initialJob.job.title,city:initialJob.job.locations.join('、'),recruitment_cycle:initialJob.job.recruitment_type,source_url:initialJob.job.url})}},[initialJob?.job.id,initialJob?.job.url])
  useEffect(()=>{if(initialResumeId&&!snapshot)setSelectedResume(initialResumeId)},[initialResumeId])
  useEffect(()=>{setRegistrationEmail(profile?.email||'');setRegistrationPhone(profile?.phone||'')},[profile?.email,profile?.phone])
  const resetReview=()=>{setPlan(null);setComparisons([]);setExecution(null);setCheck(null);setAnswers({});setSkippedSelectors(new Set());setCheckedSkips(null);setEditingSelectors(new Set());setPhaseProgress({rules:null,model:null});setAssistResult(null);setJourneyResult(null);setMemoryFailures([]);setAllowSiteParse(false)}
  const applyAgentTurn=(turn:ApplicationAgentTurn)=>{if(turn.action_taken&&turn.action_taken!=='analyze_and_fill')resetReview();setAgentTurn(turn);setWorkflow(turn.workflow);setSnapshot(turn.snapshot);if(turn.review){setPlan(turn.review.plan);setComparisons(turn.review.comparisons)}if(turn.assistance){setAssistResult(turn.assistance);setExecution(null);setAllowSiteParse(false);setPlan(turn.assistance.review?.plan??null);setComparisons(turn.assistance.review?.comparisons??[])}if(turn.execution)setExecution(turn.execution);setCheck(turn.assistance?.pre_submit??turn.pre_submit??turn.execution?.pre_submit??null)}
  const start=async()=>{
    if(busy||snapshot||browserOccupied||!ensureDraftSettled())return
    if(!backendReady){notify('暂时无法连接职达后端，请检查后重试。');return}
    const destination=inspectTaskUrl(url)
    if(destination.error){notify(destination.error);return}
    if(!openedSafari||openedSafari.url!==destination.url){notify('请先连接你指定的已登录填写窗口，职达不会另开窗口。');return}
    setBusy('opening');resetReview();setNativeImport(null);setWorkflow(null);setAgentTurn(null)
    try{
      // This form-only workbench never launches a new window or job navigation.
      const data=await api.startBrowser(destination.url,undefined,selectedResume,openedSafari?.url===destination.url?openedSafari.token:'')
      setSnapshot(data);setRecoveryWarning('')
      const state=await api.workflowState(data.session_id)
      setWorkflow(state)
      return {snapshot:data,workflow:state}
    }catch(error){if(error instanceof ApiError&&error.status===404)setOpenedSafari(null);notify(message(error))}
    finally{setBusy('')}
  }
  const refreshWorkflow=async()=>{if(!snapshot||busy||!ensureDraftSettled())return;setBusy('refreshing');try{const [state,fresh]=await Promise.all([api.advanceWorkflow(snapshot.session_id,'refresh'),api.browserSnapshot(snapshot.session_id)]);setWorkflow(state);setSnapshot(fresh);setObservedChange(null);setNativeImport(null);resetReview();notify(`已重新识别，当前阶段：${workflowStageLabel(state.stage)}。`);assessInBackground(snapshot.session_id)}catch(e){notify(message(e))}finally{setBusy('')}}
  const assessAgent=async()=>{if(!snapshot||busy||!ensureCurrentPage()||!ensureDraftSettled())return;setBusy('agent-assessing');try{applyAgentTurn(await api.assessApplicationAgent(snapshot.session_id));notify('流程 Agent 已结合当前网页、资料准备度和安全边界给出下一步。')}catch(e){notify(message(e))}finally{setBusy('')}}
  const runAgentStep=async()=>{if(!snapshot||busy||!ensureCurrentPage()||!ensureDraftSettled())return;setBusy('agent-running');try{const turn=await api.runApplicationAgentStep(snapshot.session_id,selectedResume);applyAgentTurn(turn);if(turn.assistance){await syncTrackedStatus('needs_review');notify(`${assistStatus(turn.assistance.status).title}：${turn.assistance.message}`)}else if(turn.execution){const synced=await syncTrackedStatus(trackedStatus(turn.execution.pre_submit,turn.execution.failed));notify(`Agent 已填写并回读验证 ${turn.execution.verified} 项，${turn.execution.failed} 项失败。${synced?'流水账已同步。':''}`)}else notify(turn.action_taken?`Agent 已执行：${agentActionLabel(turn.action_taken)}`:turn.decision.next_label)}catch(e){notify(message(e))}finally{setBusy('')}}
  const runJourney=async()=>{
    if(!snapshot)return
    const blocker=journeyGuard({busy:Boolean(busy),available:backendReady&&journeyReady,resumeId:selectedResume,hasDraftEdits})
    if(blocker){notify(blocker);return}
    // The server observes afresh before every action, including after manual login.
    // No previous selector, credentials, upload permission, or form action is sent.
    setBusy('journey');setJourneyResult(null);setAllowSiteParse(false)
    try{
      const result=await api.runApplicationJourney(snapshot.session_id,selectedResume)
      applyAgentTurn(result.turn);setJourneyResult(result);setObservedChange(null);setRecoveryWarning('')
      if(result.turn.assistance||result.turn.execution)await syncTrackedStatus('needs_review')
      notify(`${journeyStatus(result.status)}：${result.message}`)
    }catch(error){setPlan(null);setComparisons([]);setCheck(null);setWorkflow(null);setRecoveryWarning('本轮未完成，当前招聘页面状态需要重新确认。请先只读同步并核对官网实际内容；不会自动重试或重复上传附件。');notify(`本轮未完成：${message(error)}`)}
    finally{setBusy('')}
  }
  const onFactSaved=async(updated:ResumeRecord):Promise<string>=>{
    onResumeUpdate(updated);setPlan(null);setComparisons([]);setExecution(null);setCheck(null);setAssistResult(null);setJourneyResult(null);setAgentTurn(null)
    if(!snapshot)return '真实资料已保存到所选简历，之后可跨公司复用；附件原文件未修改。'
    try{
      const fresh=await api.reviewForm(snapshot.session_id)
      if(!draftFieldsCompatible([...Object.keys(answers),...skippedSelectors],snapshot,fresh.snapshot)){
        setRecoveryWarning('真实资料已保存，但网页题目或经历归属发生变化。临时答案仍保留，旧计划已暂停；请先核对再同步当前页。')
        try{setObservedChange(await api.workflowState(snapshot.session_id))}catch{setWorkflow(null)}
        return '真实资料已入库；旧计划已停用。请先核对保留的临时答案，再同步页面。附件未修改。'
      }
      setSnapshot(fresh.snapshot);setPlan(fresh.plan);setComparisons(fresh.comparisons)
      return '真实资料已入库并重新核对，未执行的临时答案保留。附件未修改，也未自动填写招聘网页。'
    }catch{setWorkflow(null);setRecoveryWarning('真实资料已入库，但重新核对失败。旧填写计划已停用，临时答案仍保留；请只读同步当前页。');return '保存成功，但重新核对未完成，请同步当前页后继续。附件未修改。'}
  }
  const enterApplication=async()=>{if(!snapshot||busy||!ensureCurrentPage()||!ensureDraftSettled())return;setBusy('advancing');try{const state=await api.advanceWorkflow(snapshot.session_id,'start_application');setWorkflow(state);setSnapshot(await api.browserSnapshot(snapshot.session_id));notify(state.message)}catch(e){notify(message(e))}finally{setBusy('')}}
  const navigateCandidate=async(candidate:NavigationCandidate)=>{if(!snapshot||busy||!ensureCurrentPage()||!ensureDraftSettled())return;const destination=inspectNavigation(candidate,snapshot.url);if(destination.error){notify(destination.error);return}if(!window.confirm(`确认${navigationAction(candidate.kind)}：“${candidate.label}”？\n${destination.samePage?'使用当前网页中的已识别控件':'目的域名'}：${destination.hostname}\n不会提交申请。`))return;setBusy('navigating');try{const state=await api.advanceWorkflow(snapshot.session_id,candidate.kind,candidate.id);setWorkflow(state);setSnapshot(await api.browserSnapshot(snapshot.session_id));resetReview();setAgentTurn(null);notify(state.message);assessInBackground(snapshot.session_id)}catch(error){notify(message(error))}finally{setBusy('')}}
  const requestCode=async()=>{if(!snapshot||!workflow||busy||!ensureCurrentPage())return;setBusy('requesting-code');try{const channel=workflow.verification_channel==='email'?'email':'phone';const value=workflow.stage==='registration_required'?(channel==='email'?registrationEmail:registrationPhone):'';setWorkflow(await api.requestVerificationCode(snapshot.session_id,channel,value));notify(`已使用当前${channel==='email'?'邮箱':'手机号'}请求验证码。`)}catch(e){notify(message(e))}finally{setBusy('')}}
  const enterCode=async(submit:boolean)=>{if(!snapshot||!verificationCode||busy||!ensureCurrentPage())return;setBusy('verifying');try{const state=await api.enterVerificationCode(snapshot.session_id,verificationCode,submit);setVerificationCode('');setWorkflow(state);setSnapshot(await api.browserSnapshot(snapshot.session_id));notify(submit?'验证码已填入并尝试继续。':'验证码已填入，等待你在浏览器中确认。')}catch(e){notify(message(e))}finally{setVerificationCode('');setBusy('')}}
  const fillRegistration=async()=>{if(!snapshot||!registrationPassword||busy||!ensureCurrentPage())return;setBusy('registering');try{setWorkflow(await api.fillRegistration(snapshot.session_id,registrationEmail,registrationPhone,registrationPassword));setSnapshot(await api.browserSnapshot(snapshot.session_id));notify('注册信息已填入 Chrome；密码已从智达页面内存清除，请去 Chrome 核对并亲自勾选协议。')}catch(e){notify(message(e))}finally{setRegistrationPassword('');setBusy('')}}
  const createAccount=async()=>{if(!snapshot||busy||!ensureCurrentPage())return;setBusy('creating-account');try{const state=await api.advanceWorkflow(snapshot.session_id,'create_account');setWorkflow(state);setSnapshot(await api.browserSnapshot(snapshot.session_id));notify(`已执行创建账号，当前阶段：${workflowStageLabel(state.stage)}。`)}catch(e){notify(message(e))}finally{setBusy('')}}
  const analyze=async()=>{if(!snapshot||busy||!ensureCurrentPage()||!ensureDraftSettled())return;setBusy('planning');resetReview();try{const result=await api.reviewForm(snapshot.session_id);setSnapshot(result.snapshot);setPlan(result.plan);setComparisons(result.comparisons);notify(`核对完成：${result.summary.matched} 项一致，${result.summary.missing} 项漏填，${result.summary.conflict} 项冲突。`)}catch(e){notify(message(e))}finally{setBusy('')}}
  const refreshKnowledgePlan=async(selector?:string)=>{if(!snapshot||!ensureCurrentPage())return;setBusy('planning');try{if(selector){const source=snapshot.fields.find(field=>field.selector===selector);const affected=new Set(snapshot.fields.filter(field=>field.selector===selector||source?.field_type==='radio'&&field.field_type==='radio'&&(source.control_group_key?field.control_group_key===source.control_group_key:!!source.group_label&&field.group_label===source.group_label&&field.section===source.section)).map(field=>field.selector));setAnswers(previous=>Object.fromEntries(Object.entries(previous).filter(([key])=>!affected.has(key))))}setExecution(null);setCheck(null);const result=await api.reviewForm(snapshot.session_id);setSnapshot(result.snapshot);setPlan(result.plan);setComparisons(result.comparisons)}finally{setBusy('')}}
  const analyzeWithAI=async()=>{if(!snapshot||busy||!ensureCurrentPage()||!ensureDraftSettled())return;setBusy('ai-planning');resetReview();try{const result=await api.reviewForm(snapshot.session_id,true);setSnapshot(result.snapshot);setPlan(result.plan);setComparisons(result.comparisons);notify(`AI 深度分析完成：仍有 ${result.summary.manual_review+result.summary.unmapped+result.summary.option_unavailable} 项需要人工处理。`)}catch(e){notify(message(e))}finally{setBusy('')}}
  const importWithSite=async()=>{if(!snapshot||!selectedResume||busy||!ensureCurrentPage()||!ensureDraftSettled())return;if(!window.confirm('将当前选择的简历上传给招聘网站解析。解析可能覆盖网页已有填写；之后会重新核对，不会同意声明或提交申请。继续吗？'))return;setBusy('site-parsing');resetReview();try{const imported=await api.importResumeWithSite(snapshot.session_id,selectedResume,true);setNativeImport(imported);setSnapshot(imported.snapshot);notify(imported.message);const result=await api.reviewForm(snapshot.session_id);setSnapshot(result.snapshot);setPlan(result.plan);setComparisons(result.comparisons)}catch(e){notify(message(e))}finally{setBusy('')}}
  const expandSection=async(field:PageField)=>{if(!snapshot||busy||!ensureCurrentPage()||!ensureDraftSettled())return;setBusy('expanding');try{await api.expandBrowserSection(snapshot.session_id,field.selector,field.name.startsWith('autohome-section:')?field.name.slice('autohome-section:'.length):'');setAnswers({});setSkippedSelectors(new Set());setEditingSelectors(new Set());setExecution(null);setCheck(null);const result=await api.reviewForm(snapshot.session_id);setSnapshot(result.snapshot);setPlan(result.plan);setComparisons(result.comparisons);notify(`已在招聘网页展开“${fieldDisplayLabel(field)}”，并重新核对 ${result.snapshot.fields.length} 个字段。`)}catch(e){notify(message(e))}finally{setBusy('')}}
  const inspectField=async(field:PageField)=>{if(!snapshot||busy||!ensureCurrentPage())return;setBusy(`inspecting:${field.selector}`);try{const result=await api.inspectBrowserField(snapshot.session_id,field.selector);if(!draftFieldsCompatible([...Object.keys(answers),...skippedSelectors],snapshot,result.snapshot)){setObservedChange(await api.workflowState(snapshot.session_id));notify('已定位网页，但题目、选项或记录归属发生变化。你的临时答案仍保留，旧填写计划已暂停；请先核对再同步。');return}setSnapshot(result.snapshot);setPlan(result.plan);setComparisons(result.comparisons);setExecution(null);setCheck(null);const refreshed=result.snapshot.fields.find(item=>item.selector===field.selector);notify(refreshed?.options.length?`已定位“${fieldDisplayLabel(refreshed)}”，并读取到 ${refreshed.options.length} 个真实选项。`:`已在招聘网页中高亮“${fieldDisplayLabel(refreshed||field)}”，但网站暂未公开选项，请在网页中点开后再重新核对。`)}catch(e){notify(message(e))}finally{setBusy('')}}
  const answer=(action:FillAction,value:string)=>{setCheckedSkips(null);if(value!==answers[action.selector])setMemoryFailures(current=>current.filter(selector=>selector!==action.selector));setAnswers(current=>{const next={...current};if(value)next[action.selector]=value;else delete next[action.selector];return next})}
  const radioKey=(field:PageField)=>field.control_group_key||field.group_label||field.name||field.label
  const answerRadio=(field:PageField,selector:string)=>{setCheckedSkips(null);const group=snapshot?.fields.filter(item=>item.field_type==='radio'&&radioKey(item)===radioKey(field))??[];setMemoryFailures(current=>current.filter(item=>!group.some(field=>field.selector===item)));setAnswers(current=>{const next={...current};group.forEach(item=>delete next[item.selector]);if(selector)next[selector]='true';return next})}
  const answerMulti=(action:FillAction,option:string,checked:boolean)=>{setCheckedSkips(null);setMemoryFailures(current=>current.filter(selector=>selector!==action.selector));setAnswers(current=>{const values=new Set(split(current[action.selector]||''));checked?values.add(option):values.delete(option);const next={...current};const value=[...values].join('，');if(value)next[action.selector]=value;else delete next[action.selector];return next})}
  const answerCheckboxGroup=(group:CheckboxQuestionGroup,selector:string,checked:boolean)=>{if(group.blockReason)return;setCheckedSkips(null);const members=new Set(group.fields.map(field=>field.selector));setMemoryFailures(current=>current.filter(item=>!members.has(item)));setAnswers(current=>checkboxGroupAnswers(group,current,selector,checked))}
  const toggleSkip=(selector:string)=>{setCheckedSkips(null);setMemoryFailures(current=>current.filter(item=>item!==selector));setSkippedSelectors(current=>{const next=new Set(current);next.has(selector)?next.delete(selector):next.add(selector);return next})}
  const toggleEdit=(selector:string)=>setEditingSelectors(current=>{const next=new Set(current);next.has(selector)?next.delete(selector):next.add(selector);return next})
  const reusableValue=(field:PageField,raw:string)=>field.field_type==='radio'?(field.option_label||field.option_value||field.label):field.field_type==='checkbox'?(raw==='true'?'是':'否'):raw.trim()
  const persistAnswer=async(action:FillAction,raw:string)=>{
    const field=snapshot?.fields.find(f=>f.selector===action.selector)
    if(!field||action.sensitive||skippedSelectors.has(action.selector))return null
    const groupedCheckbox=field.field_type==='checkbox'&&checkboxQuestionGroups(snapshot?.fields??[]).some(group=>group.fields.some(member=>member.selector===field.selector))
    if(groupedCheckbox&&(!field.field_signature?.startsWith('checkbox-option-v1:')||field.knowledge_block_reason?.startsWith('复选题记忆归属不明确：')))return null
    const value=reusableValue(field,raw);if(!value)return null
    const question=fieldDisplayLabel(field,action.label)
    return api.saveApplicationAnswer(question,field.name,value,{semantic_key:field.semantic_key,entity_scope:field.entity_scope,field_signature:field.field_signature,field_type:field.field_type,options:field.options,source_url:snapshot?.url||'',resume_id:selectedResume})
  }
  const remember=async(action:FillAction)=>{
    if(busy||!snapshot||!ensureCurrentPage())return
    const value=answers[action.selector]?.trim()
    if(!value||!snapshot.fields.some(field=>field.selector===action.selector))return
    setBusy('remembering')
    let saved=false
    try{
      // A failed memory write may be retried long after the browser/resume changed.
      // Check the current identity before persisting any retained answer.
      const current=await api.currentBrowser()
      const currentSnapshot=current.session_id===snapshot.session_id?await api.browserSnapshot(snapshot.session_id):null
      if(current.resume_id!==selectedResume||!currentSnapshot||!draftFieldsCompatible([...Object.keys(answers),...skippedSelectors],snapshot,currentSnapshot)){
        setPlan(null);setComparisons([]);setCheck(null);setExecution(null);setObservedChange(workflow);setWorkflow(null)
        setRecoveryWarning('网页题目、选项或本次简历已变化，尚未保存答案记忆。临时答案仍保留，旧计划已暂停；请核对后只读同步当前页。')
        notify('当前页面或简历与原答案不一致，未写入记忆；请先核对保留的答案。')
        return
      }
      const currentField=currentSnapshot.fields.find(field=>field.selector===action.selector)
      if(currentField?.field_type==='checkbox'&&currentField.current_value!==value){
        notify('官网勾选状态与保留答案不一致，未写入记忆。请先核对并重新填写验证。')
        return
      }
      const updated=await persistAnswer(action,value)
      if(!updated)return
      saved=true;onProfileUpdate(updated)
      if(memoryFailures.includes(action.selector)){
        setAnswers(previous=>{const next={...previous};delete next[action.selector];return next})
        setEditingSelectors(previous=>new Set([...previous].filter(selector=>selector!==action.selector)))
      }
      setMemoryFailures(previous=>previous.filter(selector=>selector!==action.selector))
      // Persistence changes the context token even when the following review fails.
      setPlan(null);setComparisons([]);setCheck(null);setExecution(null)
      const fresh=await api.reviewForm(snapshot.session_id)
      if(fresh.plan.resume_id===selectedResume&&draftFieldsCompatible([...Object.keys(answers),...skippedSelectors],snapshot,fresh.snapshot)){
        setSnapshot(fresh.snapshot);setPlan(fresh.plan);setComparisons(fresh.comparisons)
        notify('已记住本次简历在本网站的答案，并重新核对；不是修改简历原始事实，未执行的输入仍保留。')
      }else{
        setObservedChange(workflow);setWorkflow(null)
        setRecoveryWarning('答案已保存，但网页题目、选项或简历发生变化，旧填写计划已暂停。请只读同步当前页。')
        notify('答案已保存，但当前页面发生变化；旧计划已停用，请同步当前页。')
      }
    }catch(e){
      if(saved){setWorkflow(null);setRecoveryWarning('答案已保存，但重新核对失败，旧填写计划已停用。未执行的临时答案仍保留；请只读同步当前页。');notify(`答案已保存，但重新核对未完成：${message(e)}`)}
      else notify(`未能保存答案记忆，临时答案仍保留，请重试：${message(e)}`)
    }finally{setBusy('')}
  }
  const learnConfirmedAnswers=async(result:ExecutionResult)=>{
    let updated:CandidateProfile|null=null
    if(!plan)return {count:0,failed:[]}
    const learned=await learnVerifiedDrafts(answers,result,async(selector,value)=>{
      const action=plan.actions.find(item=>item.selector===selector)
      if(!action||action.sensitive||skippedSelectors.has(selector))return false
      const field=snapshot?.fields.find(item=>item.selector===selector)
      const receipt=result.results.find(item=>item.selector===selector)
      if(field?.field_type==='checkbox'&&receipt?.actual_value!==value)throw new Error('复选项回读状态与确认答案不一致，未记住该答案')
      const saved=await persistAnswer(action,value)
      if(saved)updated=saved
      return Boolean(saved)
    })
    if(updated)onProfileUpdate(updated)
    setMemoryFailures(learned.failed)
    return learned
  }
  const preparedActions=useMemo(()=>plan?.actions.map(action=>{if(skippedSelectors.has(action.selector))return {...action,action:'skip' as const,value:'',reason:'用户选择本次不填写'};const manual=answers[action.selector];if(!manual)return action;const field=snapshot?.fields.find(f=>f.selector===action.selector);const kind=field?.field_type;return {...action,action:manualFillAction(field),value:kind==='checkbox'||kind==='radio'?manual==='true':manual,confidence:1,sensitive:action.sensitive,user_confirmed:true,resolution_source:'user' as const,needs_model:false,value_source:'用户在提交前补充'}})??[],[plan,answers,snapshot,skippedSelectors])
  const readyCount=preparedActions.filter(a=>['fill','select','check'].includes(a.action)&&(a.action==='check'||String(a.value).trim())&&(!a.sensitive||a.user_confirmed)&&a.confidence>=.85).length
  const needsInput=plan?.actions.filter(action=>{const field=snapshot?.fields.find(f=>f.selector===action.selector);if(!field||field.field_type==='file'||action.needs_model&&!answers[action.selector])return false;const emptyAction=!['fill','select','check'].includes(action.action)||(action.action!=='check'&&!String(action.value).trim());const websiteFilled=field.field_type==='radio'?Boolean(snapshot?.fields.some(item=>item.field_type==='radio'&&radioKey(item)===radioKey(field)&&item.current_value==='true')):field.field_type==='checkbox'?field.current_value==='true':Boolean(field.current_value&&!/^(select|choose|请选择|未选择|暂未选择)/i.test(field.current_value));return Boolean(answers[action.selector])||action.action==='ask_user'||action.sensitive||Boolean(field.required&&emptyAction&&!websiteFilled)})??[]
  const manualGroups=useMemo(()=>{
    const groups:{key:string;label:string;kind:'radio'|'checkbox'|'field';actions:FillAction[];fields:PageField[];checkboxGroup?:CheckboxQuestionGroup}[]=[]
    const seen=new Set<string>(),checkboxBySelector=new Map(checkboxQuestionGroups(snapshot?.fields??[]).flatMap(group=>group.fields.map(field=>[field.selector,group] as const)))
    for(const action of needsInput){
      const field=snapshot?.fields.find(item=>item.selector===action.selector);if(!field)continue
      const checkboxGroup=checkboxBySelector.get(field.selector)
      if(checkboxGroup){
        if(seen.has(checkboxGroup.key))continue;seen.add(checkboxGroup.key)
        const selectors=new Set(checkboxGroup.fields.map(item=>item.selector))
        groups.push({key:checkboxGroup.key,label:checkboxGroup.question,kind:'checkbox',fields:checkboxGroup.fields,actions:needsInput.filter(item=>selectors.has(item.selector)),checkboxGroup})
      }else if(field.field_type==='radio'){
        const key=`radio:${radioKey(field)}`;if(seen.has(key))continue;seen.add(key)
        const fields=snapshot?.fields.filter(item=>item.field_type==='radio'&&radioKey(item)===radioKey(field))??[],selectors=new Set(fields.map(item=>item.selector))
        groups.push({key,label:actionReviewTitle(action,field),kind:'radio',fields,actions:needsInput.filter(item=>selectors.has(item.selector))})
      }else groups.push({key:action.selector,label:actionReviewTitle(action,field),kind:'field',fields:[field],actions:[action]})
    }
    return groups
  },[needsInput,snapshot])
  const visibleActions=useMemo(()=>{const actions=plan?.actions??[];const result:FillAction[]=[];const seen=new Set<string>();for(const action of actions){const field=snapshot?.fields.find(item=>item.selector===action.selector);if(field?.field_type!=='radio'){result.push(action);continue}const key=radioKey(field);if(seen.has(key))continue;seen.add(key);const selectors=new Set(snapshot?.fields.filter(item=>item.field_type==='radio'&&radioKey(item)===key).map(item=>item.selector)??[]);const groupActions=actions.filter(item=>selectors.has(item.selector));result.push(groupActions.find(item=>item.action==='check')??groupActions.find(item=>item.action==='ask_user')??groupActions.find(item=>item.action!=='skip')??action)}return result},[plan,snapshot])
  const pendingModelSelectors=pendingActionSelectors(preparedActions)
  const visibleComparisons=comparisons.filter(item=>!pendingModelSelectors.has(item.selector)&&(showMatched||item.status!=='matched'))
  const groupUnanswered=(group:typeof manualGroups[number])=>group.kind==='checkbox'? !checkboxGroupAnswered(group.checkboxGroup!,answers):group.kind==='radio'?!group.fields.some(field=>answers[field.selector]==='true'):!answers[group.actions[0]?.selector]&&!skippedSelectors.has(group.actions[0]?.selector)
  const unresolvedGroups=manualGroups.filter(groupUnanswered)
  const pendingModelCount=pendingModelSelectors.size
  const hasDraftEdits=Object.values(answers).some(value=>value.trim())||skippedSelectors.size>0
  const ensureDraftSettled=()=>{if(!hasDraftEdits)return true;notify(stalePage?'招聘网页已变化，你的临时答案仍完整保留。可以先查看或复制保留的答案；若确认不再需要，请明确“清除之前的临时修改”后再同步。不会拿旧答案填入新页面。':'请先完成下方“填写并验证”，或明确“清除本页临时修改”。当前答案和“不填写”选择会保留，不会被重新分析或自动步骤覆盖。');return false}
  const ensureCurrentPage=()=>{if(!backendReady){notify('本轮自动补齐升级尚未加载，请先保留招聘草稿，再重启后端。当前仅允许读取页面状态。');return false}if(!stalePage)return true;notify('招聘网页已变化，旧填写计划已暂停。请先点击“同步当前页面”；你的临时答案不会自动清除。');return false}
  const pageCheckReady=!stalePage&&!busy&&formStageReady(workflow,snapshot)&&Boolean(check?.ready)&&unresolvedGroups.length===0&&!pendingModelCount&&!execution?.failed&&(!assistResult||assistResult.status==='ready_for_review')
  const overallReady=pageCheckReady&&!hasDraftEdits
  // Checked skips are local, page-specific consent for this manual button only.
  // Automatic journey/analysis still sees hasDraftEdits and remains blocked.
  const manualNextReady=pageCheckReady&&(!hasDraftEdits||skipsChecked&&!Object.entries(answers).some(([selector,value])=>value.trim()&&!skippedSelectors.has(selector)))
  const continueStep=async()=>{
    if(!snapshot||busy||!ensureCurrentPage()||!manualNextReady||!canContinueWorkflow(workflow))return
    setBusy('continuing')
    try{
      if(skippedSelectors.size){
        const [current,fresh]=await Promise.all([api.currentBrowser(),api.browserSnapshot(snapshot.session_id)])
        if(current.session_id!==snapshot.session_id||current.resume_id!==selectedResume||snapshot.fields.length!==fresh.fields.length||!draftFieldsCompatible(snapshot.fields.map(field=>field.selector),snapshot,fresh)){
          setCheckedSkips(null);setPlan(null);setCheck(null);setObservedChange(workflow);setWorkflow(null)
          setRecoveryWarning('本页题目或简历已变化，之前核对的“不填写”不能继续沿用。临时答案和跳过选择仍保留，请先同步并重新核对。')
          return
        }
      }
      const state=await api.advanceWorkflow(snapshot.session_id,'continue_application')
      const fresh=await api.browserSnapshot(snapshot.session_id)
      setWorkflow(state);setSnapshot(fresh);setNativeImport(null);resetReview();notify(`已安全进入下一页，当前阶段：${workflowStageLabel(state.stage)}。`)
    }catch(e){setCheckedSkips(null);notify(message(e))}finally{setBusy('')}
  }
  const trackedStatus=(next:PreSubmitCheck,failed=0):QueueStatus=>next.ready&&!failed&&workflow?.stage==='review'?'ready_to_submit':next.ready&&!failed?'in_progress':'needs_review'
  const syncTrackedStatus=async(status:QueueStatus)=>{if(!initialQueueId||!initialJob||initialJob.job.url!==snapshot?.url)return true;try{await api.updateQueuedJob(initialQueueId,{status});return true}catch{return false}}
  const discardDraft=()=>{if(!hasDraftEdits||!window.confirm('清除本页尚未执行的临时答案和“不填写”选择？已保存的主档案、记忆和招聘网页内容不会被修改。'))return;setAnswers({});setSkippedSelectors(new Set());setCheckedSkips(null);setEditingSelectors(new Set());setMemoryFailures([]);notify('本页临时修改已清除。现在可以重新智能分层填写。')}
  // Only the bounded assist endpoint can carry explicit leave-blank choices.
  // Journey, analysis and next-page guards continue to block ALL draft edits.
  const assistBlocker=assistGuard({busy:Boolean(busy),backendReady:backendReady&&recordCompletionReady,hasSnapshot:Boolean(snapshot),formReady:formStageReady(workflow,snapshot),stalePage,hasDraftEdits:Object.values(answers).some(value=>value.trim()),resumeId:selectedResume,planResumeId:plan?.resume_id})
  const assistApplication=async(connection?:{snapshot:BrowserSnapshot;workflow:ApplicationWorkflowState})=>{
    // A freshly connected page is explicit context, not an old React closure.
    // Restoring the screen never sets this argument and never starts filling.
    const currentSnapshot:BrowserSnapshot|null=connection?.snapshot??observationState.current.snapshot
    const blocker=connection?assistGuard({busy:Boolean(busy),backendReady:backendReady&&recordCompletionReady,hasSnapshot:true,formReady:formStageReady(connection.workflow,currentSnapshot),stalePage:false,hasDraftEdits:Object.values(answers).some(value=>value.trim()),resumeId:selectedResume}):assistBlocker
    if(blocker||!currentSnapshot){if(blocker)notify(connection?'请确认已经进入信息填写页，再重新同步。':blocker);return}
    const snapshot=currentSnapshot
    if(allowSiteParse&&snapshot.browser_engine!=='safari'&&!window.confirm(`将“${resumes.find(item=>item.id===selectedResume)?.label||'本次所选简历'}”上传给当前招聘网站解析，可能覆盖网页已有填写。随后会补齐并回读核对；不会勾选协议或最终提交。确认继续？`))return
    setBusy('assisting');setAssistResult(null);setExecution(null);setCheck(null);setPhaseProgress({rules:null,model:null})
    const run={session:snapshot.session_id,id:crypto.randomUUID(),active:true}
    assistRun.current=run
    rememberAssistRun(window.sessionStorage,ownerId,{session:run.session,id:run.id,started:Date.now()})
    setAssistProgress({run_id:run.id,status:'running',phase:'observe',message:'正在等待填写任务启动；若当前网页检查尚未结束，会先等待，尚未开始填写',events:[],result:null,cancel_requested:false})
    // Start one write request. A parallel metadata poll stays responsive even
    // while the browser is locked by model analysis or option verification.
    const watching=watchAssist(()=>api.assistProgress(run.session,run.id),p=>{if(run.active)setAssistProgress(p)},()=>run.active)
      .then(result=>({result,error:null}),error=>({result:null,error}))
    try{
      const deferredFields=[...skippedSelectors].map(selector=>snapshot.fields.find(field=>field.selector===selector))
      if(deferredFields.some(field=>!field))throw new Error('本次留空的字段已变化，请先同步核对，未启动填写。')
      const result=await api.assistApplication(snapshot.session_id,assistRequest(selectedResume,allowSiteParse&&snapshot.browser_engine!=='safari',deferredFields as PageField[]),run.id).catch(async error=>{
        const receipt=await watching
        if(receipt.result)return receipt.result
        throw assistFailure(error,receipt.error)
      })
      run.active=false
      forgetAssistRun(window.sessionStorage,ownerId)
      setAssistProgress({run_id:run.id,status:'finished',phase:result.status,message:result.message,events:result.events,result,cancel_requested:false})
      setAssistResult(result);setSnapshot(result.snapshot);setPlan(result.review?.plan??null);setComparisons(result.review?.comparisons??[]);setCheck(result.pre_submit);setAgentTurn(null)
      // An upload is one-shot consent, not permission to overwrite on future runs.
      setAllowSiteParse(false)
      // Server accepted the exact, observed deferrals; keep its scoped skips
      // in the returned plan, not an old DOM selector across newly added rows.
      setSkippedSelectors(new Set());setCheckedSkips(null)
      try{setWorkflow(await api.workflowState(snapshot.session_id));setObservedChange(null)}catch{setWorkflow(null);setRecoveryWarning('补齐结果已保留，但流程状态读取失败。请重新读取浏览器状态后再继续。')}
      await syncTrackedStatus('needs_review')
      notify(result.status==='ready_for_review'?'请到招聘官网核对，确认后自行提交。':'本轮已暂停；请补充所需资料或核对官网，尚未最终提交。')
    }catch(error){setAllowSiteParse(false);setPlan(null);setComparisons([]);setWorkflow(null);setRecoveryWarning('本轮自动补齐未取得完整结果，招聘网页可能已有部分填写。请先重新读取浏览器状态，再核对；不要直接重复上传。');notify(`自动补齐未完成：${message(error)}`)}
    finally{run.active=false;setBusy('')}
  }
  const pauseAssist=async()=>{
    const run=assistRun.current
    if(!run?.active)return
    try{setAssistProgress(await api.cancelAssist(run.session,run.id));notify('已请求暂停；当前读取或模型调用结束后停止后续填写，已填内容保留。')}
    catch(error){notify(`暂停请求未确认：${message(error)}；请先不要改动网页。`)}
  }
  const confirmMonthPrecision=async()=>{
    if(assistBlocker||!snapshot||!needsMonthPrecisionConsent(snapshot.url,plan)){if(assistBlocker)notify(assistBlocker);return}
    if(!window.confirm('仅对本次所选简历在汽车之家网申使用：简历只有年月的经历日期，允许将每月1日作为月份占位；这不是精确发生日期，“至今”不变，也不会修改简历原始日期。若你知道精确日期，请取消并逐项填写。确认允许？'))return
    setBusy('date-precision')
    try{
      const updated=await api.saveApplicationAnswer(monthPrecisionMemory.question,'',monthPrecisionMemory.value,{semantic_key:monthPrecisionMemory.semantic_key,entity_scope:monthPrecisionMemory.entity_scope,source_url:snapshot.url,resume_id:selectedResume})
      onProfileUpdate(updated)
      const fresh=await api.reviewForm(snapshot.session_id)
      setSnapshot(fresh.snapshot);setPlan(fresh.plan);setComparisons(fresh.comparisons);setCheck(null);setExecution(null);setAssistResult(null)
      notify('已记住本次简历在该网站的月份占位约定，并重新分析。原始简历日期未修改；点击“自动补齐并核对”继续。')
    }catch(error){notify(`日期约定处理未完成：${message(error)}`)}finally{setBusy('')}
  }
  const smartAutofill=async()=>{
    if(!snapshot||busy||!ensureCurrentPage())return
    if(hasDraftEdits){notify('当前有你手动修改的答案或“不填写”选择。请先点击下方“填写并验证”，或明确清除临时修改，再启动智能分层填写；系统不会覆盖你的修改。');return}
    setPhaseProgress({rules:null,model:null});setExecution(null);setCheck(null);setAssistResult(null);setBusy('autofill-rules')
    let cumulative:ExecutionResult|null=null
    let rulesComplete=false
    try{
      const completed=await runAutofillPhases(phase=>api.autofillPhase(snapshot.session_id,phase),(result,combined)=>{
        rulesComplete=true;cumulative=combined
        setSnapshot(result.review.snapshot);setPlan(result.review.plan);setComparisons(result.review.comparisons);setExecution(combined);setCheck(combined.pre_submit)
        setPhaseProgress(previous=>({...previous,[result.phase]:result.execution.verified}))
      },()=>{setBusy('autofill-model');notify('规则阶段已经完成，结果已显示。模型正在处理剩余歧义字段，无须先逐项回答。')})
      const {first,latest}=completed
      cumulative=completed.execution
      const stillPending=modelPending(latest.review.plan)
      const synced=await syncTrackedStatus(stillPending?'needs_review':trackedStatus(cumulative.pre_submit,cumulative.failed))
      notify(`分层填写完成：规则阶段 ${first.execution.verified} 项${latest.phase==='model'?`，模型阶段 ${latest.execution.verified} 项`:''}通过回读；累计 ${cumulative.failed} 项失败。${stillPending?`仍有 ${stillPending} 项待模型处理。`:'请只处理下方仍需你确认的问题，并做最终人工核对。'}未上传附件，也未点击提交。${synced?'流水账已同步。':'流水账同步失败，请手动核对记录。'}`)
    }catch(error){if(cumulative)await syncTrackedStatus('needs_review');notify(`${rulesComplete?'规则阶段已经完成，已有填写结果保留；后续模型阶段未完成：':'分层填写未完成：'}${message(error)}`)}finally{setBusy('')}
  }
  const uploadAttachmentOnly=async()=>{
    if(!snapshot||busy||!canAnalyze||!selectedResume||!ensureCurrentPage()||!ensureDraftSettled())return
    const resume=resumes.find(item=>item.id===selectedResume)
    if(!resume){notify('未找到本次简历，请重新选择。');return}
    const blocked=resumeAttachmentBlocker(snapshot)
    if(blocked){notify(blocked);return}
    const host=new URL(snapshot.url).hostname
    if(!window.confirm(`将“${resume.filename}”上传到 ${host} 的简历附件栏。\n只上传附件，不点击网站解析、不改填其他字段、不勾协议、不提交申请。\n如网站自身自动处理附件，请完成后重新核对表单。是否允许？`))return
    setBusy('attachment-uploading')
    try{
      const current=await api.currentBrowser()
      if(current.session_id!==snapshot.session_id||current.resume_id!==selectedResume)throw new Error('当前任务或简历已变化，未上传。')
      const fresh=await api.reviewForm(snapshot.session_id)
      if(fresh.plan.resume_id!==selectedResume||fresh.snapshot.url!==snapshot.url||
        !draftFieldsCompatible(snapshot.fields.map(field=>field.selector),snapshot,fresh.snapshot))throw new Error('申请页结构已变化，请先同步再确认，未上传。')
      const latestBlocker=resumeAttachmentBlocker(fresh.snapshot)
      if(latestBlocker)throw new Error(latestBlocker)
      const result=await api.uploadResumeAttachment(snapshot.session_id,selectedResume,fresh.plan.context_token||'',true)
      setExecution(result);setCheck(result.pre_submit)
      const review=await api.reviewForm(snapshot.session_id)
      setSnapshot(review.snapshot);setPlan(review.plan);setComparisons(review.comparisons)
      const files=result.results.filter(item=>item.verified&&item.status==='filled')
      notify(files.length&&!result.failed?'简历附件已交给招聘网页，文件选择已回读核对。请检查官网附件处理状态；未触发网站解析、未勾协议、未提交。':'未能核实附件上传，请先检查官网处理状态，不要立即重复上传。')
    }catch(error){notify(message(error))}finally{setBusy('')}
  }
  const execute=async()=>{
    if(!snapshot||!plan||busy||!ensureCurrentPage())return
    setBusy('filling');setAssistResult(null);setCheckedSkips(null)
    try{
      const result=await api.executeForm(snapshot.session_id,preparedActions,selectedResume,plan.context_token)
      setExecution(result);setCheck(result.pre_submit)
      const learned=await learnConfirmedAnswers(result)
      // A successful write with failed memory persistence keeps the draft visible
      // so the user can retry remembering, without another browser write.
      const verified=new Set(verifiedDraftEntries(answers,result).map(([selector])=>selector).filter(selector=>!learned.failed.includes(selector)))
      const remaining=Object.fromEntries(Object.entries(answers).filter(([selector])=>!verified.has(selector)))
      // Refresh after memory writes: they change the context token. Only clear
      // drafts whose write/read-back succeeded; retain every failed answer.
      try{
        const fresh=await api.reviewForm(snapshot.session_id)
        if(draftFieldsCompatible([...Object.keys(remaining),...skippedSelectors],snapshot,fresh.snapshot)){
          setSnapshot(fresh.snapshot);setPlan(fresh.plan);setComparisons(fresh.comparisons);setAnswers(remaining)
          if(skippedSelectors.size&&result.pre_submit.ready&&!result.failed&&fresh.plan.resume_id===selectedResume)setCheckedSkips({selectors:[...skippedSelectors],snapshot:fresh.snapshot,resumeId:selectedResume})
          setEditingSelectors(previous=>new Set([...previous].filter(selector=>!verified.has(selector))))
        }else{
          setObservedChange(await api.workflowState(snapshot.session_id))
          setRecoveryWarning('填写后页面结构发生变化，未完成的临时答案仍保留；旧计划已暂停，请先核对再同步当前页面。')
        }
      }catch{setWorkflow(null);setRecoveryWarning('已完成的填写保留，但重新核对失败。临时答案未清除，请先重新读取浏览器状态，再检查剩余项。')}
      const synced=await syncTrackedStatus(result.failed||Object.keys(remaining).length?'needs_review':trackedStatus(result.pre_submit))
      if(!Object.keys(remaining).length&&!learned.failed.length)setWorkspaceSection('operation')
      notify(result.failed||Object.keys(remaining).length?'部分答案尚未完成，未保存的输入已保留，请核对后继续。':learned.failed.length?'答案尚未记住，输入仍保留；请核对官网后单独重试。':'答案已填写并核对。可回到投递操作继续；最终提交仍由你完成。')
    }catch(e){notify(message(e))}finally{setBusy('')}
  }
  const recheck=async()=>{
    if(!snapshot||busy||!ensureCurrentPage())return
    setBusy('checking');setCheckedSkips(null)
    try{
      const next=await api.checkForm(snapshot.session_id)
      if(skippedSelectors.size){
        const fresh=await api.browserSnapshot(snapshot.session_id)
        if(snapshot.fields.length!==fresh.fields.length||!draftFieldsCompatible(snapshot.fields.map(field=>field.selector),snapshot,fresh)){
          setCheck(null);setObservedChange(workflow);setWorkflow(null)
          setRecoveryWarning('检查过程中网页题目发生变化，临时答案与“不填写”选择仍保留；请先同步并重新核对。')
          return
        }
        setSnapshot(fresh)
        if(next.ready)setCheckedSkips({selectors:[...skippedSelectors],snapshot:fresh,resumeId:selectedResume})
      }
      setCheck(next)
      const synced=await syncTrackedStatus(trackedStatus(next));notify(synced?'已重新检查当前网页，流水账状态已同步。':'网页检查完成，但流水账同步失败，请返回岗位推荐页手动更新状态。')
    }catch(e){notify(message(e))}finally{setBusy('')}
  }
  const close=async()=>{if(busy)return;setBusy('closing');agentRevision.current+=1;try{if(snapshot)await api.closeBrowser(snapshot.session_id);setSnapshot(null);setOpenedSafari(null);setObservedChange(null);setBrowserOccupied(false);setRecoveryWarning('');setWorkflow(null);setAgentTurn(null);setVerificationCode('');setRegistrationPassword('');setNativeImport(null);resetReview();notify('浏览器会话已结束，可以开始新的任务。')}catch(error){if(error instanceof ApiError&&error.status===404){setSnapshot(null);setOpenedSafari(null);setObservedChange(null);setBrowserOccupied(false);setWorkflow(null);resetReview();notify('原会话已经结束。')}else notify(`结束会话失败，已保留当前任务，不会创建新任务覆盖它。${message(error)}`)}finally{setBusy('')}}
  const canAnalyze=!stalePage&&formStageReady(workflow,snapshot)
  const workbenchState=workbenchView({workflow,snapshot,hasPlan:Boolean(plan),hasDraftEdits,unresolvedQuestions:unresolvedGroups.length,ready:overallReady,hasExecution:Boolean(execution||assistResult)})
  const workspaceView={...workbenchState,label:workbenchState.action==='fill'?'自动补齐并核对':workbenchState.label}
  const focusSection=(id:string)=>document.getElementById(id)?.scrollIntoView({behavior:'smooth',block:'start'})
  const nextWorkspaceStep=()=>{if(recoveryWarning&&!hasDraftEdits){void restoreExistingSession();return}if(hasDraftEdits){focusSection('workbench-results');notify('临时答案还没有处理，请先填写并验证；若只差记忆，点击“记住本网站答案”即可。');return}if(workspaceView.action==='review'){focusSection(plan?'workbench-results':'workbench-assist-result');return}if(!stalePage&&formStageReady(workflow,snapshot)){void assistApplication();return}if(workspaceView.action==='choose'&&workspaceView.navigationCandidates.some(candidate=>candidate.requires_user_choice)){focusSection('workbench-navigation');notify('请在下方选择要申请的招聘机构；不会替你猜志愿。');return}void runJourney()}
  const userGroups=manualGroups.filter(group=>!group.checkboxGroup?.blockReason&&group.fields.every(isUserAnswerQuestion))
  const systemGroups=manualGroups.filter(group=>!!group.checkboxGroup?.blockReason||!group.fields.every(isUserAnswerQuestion))
  const userPending=userGroups.filter(groupUnanswered)
  const submitSetup=(selection:{resumeId:string;url:string})=>{
    const destination=inspectTaskUrl(selection.url)
    if(destination.error){notify(destination.error);return}
    if(snapshot&&snapshot.url!==destination.url){notify('已有填写页仍在连接。请先结束本次连接，再更换网址；不会把旧页面的答案填到新网站。');return}
    if(openedSafari?.url!==destination.url)setOpenedSafari(null)
    setUrl(destination.url);setWorkspaceSection('operation');setWorkspaceStage('workspace')
  }
  const beginFill=async()=>{
    if(snapshot){await assistApplication();return}
    if(!hasMatchingSafari){notify('请先连接你已经登录的招聘填写窗口。');return}
    const connection=await start()
    if(connection)await assistApplication(connection)
  }
  const userStatus=busy==='restoring'?'正在检查连接…':busy==='assisting'||busy==='filling'?'正在辅助填写，请暂时不要改动招聘网页。':busy?'正在处理，请稍候…':overallReady?'请到招聘官网核对，确认后自行提交。':userPending.length?'有资料需要你补充。':assistResult||execution?'填写已暂停，请核对官网；仍有项目需要处理。':snapshot?'点击下方按钮开始辅助填写。':'连接已登录的填写页后，即可开始。'
  const operation=<div className="stack">
    {recoveryWarning&&<div className="chat-url-warning" role="alert"><p>当前任务需要重新核对，未完成的答案仍保留。</p><button disabled={!!busy} onClick={restoreExistingSession}>重新检查连接</button></div>}
    {!backendReady&&!busy&&<div className="chat-url-warning" role="alert">暂时无法连接职达后端。<button onClick={restoreExistingSession}>重新检查</button></div>}
    <section className="card application-fill-card">
      <h3>辅助投递</h3><p>职达会读取题目、匹配资料并填写；不确定的答案会在“待补充资料”中询问。</p>
      <small>不会点击招聘官网的最终提交，不会替你同意声明。</small>
      {!snapshot&&!hasMatchingSafari&&<ExistingSafariConnection url={url} disabled={!!busy||browserOccupied||!backendReady} notify={notify} onBusyChange={value=>setBusy(value?'choosing-window':'')} onBound={(result,openedUrl)=>setOpenedSafari(result.safari_window_token?{url:openedUrl,token:result.safari_window_token}:null)}/>}
      {!snapshot&&hasMatchingSafari&&<p>已连接你指定的招聘窗口，不会另开或刷新。</p>}
      {snapshot&&!formStageReady(workflow,snapshot)&&!busy&&<p role="alert">还没有确认这是信息填写页。请先在官网进入填写页面，然后重新同步；不会自动登录或换岗位。</p>}
      <div className="review-buttons">
        <button className="primary" disabled={Boolean(busy)||!backendReady||!selectedResume||(!snapshot&&!hasMatchingSafari)||Boolean(snapshot&&assistBlocker)} onClick={()=>void beginFill()}>{busy==='assisting'?<LoaderCircle className="spin" size={18}/>:<WandSparkles size={18}/>} {busy==='assisting'?'正在辅助填写…':assistResult||execution?'继续辅助填写':'投递（辅助填写）'}</button>
        {snapshot&&<button className="secondary" disabled={!!busy||hasDraftEdits} onClick={restoreExistingSession}><RefreshCw size={14}/>同步当前页</button>}
        {busy==='assisting'&&<button className="secondary" onClick={pauseAssist}>暂停</button>}
      </div>
      {userPending.length>0&&<button className="secondary" disabled={!!busy} onClick={()=>setWorkspaceSection('questions')}>补充资料并继续</button>}
      {hasDraftEdits&&!busy&&<p>你补充的答案尚未处理，请到“待补充资料”保存并继续，不会覆盖这些输入。</p>}
      {snapshot&&<p className="application-attachment-note">附件、照片请在官网上传；职达不会把未上传的材料显示为已完成。</p>}
      {(systemGroups.length>0||pendingModelCount>0)&&!busy&&<p>还有网页项目未处理，不能视为填写完成。详细原因已留在开发检查中。</p>}
      {overallReady&&<p className="application-review-ready"><ShieldCheck size={18}/>当前检查允许人工终审；请在官网检查全部内容并自行提交。</p>}
    </section>
    {check&&Boolean(check.human_challenges.length||check.validation_errors.length)&&<section className="card"><h3>需要在官网处理</h3><ul>{[...check.human_challenges,...check.validation_errors].map((item,index)=><li key={index}>{item}</li>)}</ul></section>}
    <details className="workbench-extra"><summary>更换填写页或结束本次任务</summary><p>返回设置可以修改网址与简历；已有填写不会自动撤销。更换已连接的页面前，请先结束本次任务。</p><button disabled={!!busy} onClick={close}>结束本次连接</button></details>
  </div>
  const questions=<div className="stack">
    <section className="card">
      <h3>只补充缺少的真实资料</h3><p>保存后先填写并核对，核对成功、复用范围明确的非敏感答案会按题意、简历版本和网站范围记住。不会把不同学历或经历混在一起。</p>
      {!plan?<p>开始辅助填写后，需要你提供的资料会出现在这里。</p>:userGroups.length?<div className="answer-list">{userGroups.map(group=>{
        const action=group.actions[0],field=group.fields[0]
        // Display the verified website question, not a model-rewritten question.
        const label=field.question_text||field.group_label||field.label
        const selected=group.fields.find(item=>answers[item.selector]==='true')?.selector||''
        return <div className="answer-row" key={group.key}>
          <div className="field-review-copy"><div className="field-review-title"><strong>{label}</strong>{field.required&&<b>必填</b>}</div>{field.section_path?.length>0&&<p>{field.section_path.join(' / ')}</p>}</div>
          <div className="manual-control">
            {group.kind==='radio'?<select disabled={!!busy||stalePage} aria-label={label} value={selected} onChange={event=>answerRadio(field,event.target.value)}><option value="">请选择真实答案</option>{group.fields.map(option=><option key={option.selector} value={option.selector}>{option.option_label||option.option_value||option.label}</option>)}</select>
            :group.kind==='checkbox'?<fieldset className="option-checklist" aria-label={label}><legend>请选择实际符合的选项，可多选</legend>{group.fields.map(option=><label key={option.selector}><input type="checkbox" disabled={!!busy||stalePage} checked={checkboxGroupChecked(option,answers)} onChange={event=>answerCheckboxGroup(group.checkboxGroup!,option.selector,event.target.checked)}/>{option.option_label||option.option_value||option.label}</label>)}</fieldset>
            :field.field_type==='checkbox'?<select disabled={!!busy||stalePage} aria-label={label} value={answers[action.selector]||''} onChange={event=>answer(action,event.target.value)}><option value="">请选择</option><option value="true">是</option><option value="false">否</option></select>
            :field.date_precision?<input type={field.date_precision} disabled={!!busy||stalePage} aria-label={label} value={answers[action.selector]||''} onChange={event=>answer(action,event.target.value)}/>
            :field.multiple&&field.options.length?<div className="option-checklist">{field.options.map(option=><label key={option}><input type="checkbox" disabled={!!busy||stalePage} checked={split(answers[action.selector]||'').includes(option)} onChange={event=>answerMulti(action,option,event.target.checked)}/>{option}</label>)}</div>
            :field.options.length?<select disabled={!!busy||stalePage} aria-label={label} value={answers[action.selector]||''} onChange={event=>answer(action,event.target.value)}><option value="">请选择真实答案</option>{field.options.map(option=><option key={option} value={option}>{option}</option>)}</select>
            :field.field_type==='textarea'?<textarea disabled={!!busy||stalePage} aria-label={label} value={answers[action.selector]||''} onChange={event=>answer(action,event.target.value)}/>
            :<input disabled={!!busy||stalePage} aria-label={label} value={answers[action.selector]||''} onChange={event=>answer(action,event.target.value)} placeholder="请填写真实答案"/>}
            {action.sensitive&&<small>仅用于本次填写，不保存为可复用答案。</small>}
            {group.kind==='checkbox'&&group.fields.some(item=>item.knowledge_block_reason?.startsWith('复选题记忆归属不明确：'))&&<small>本页有同名问题，暂不能确定下次的复用范围；这组答案仅用于本次填写。</small>}
          </div>
        </div>
      })}</div>:<p>目前没有需要你补充的个人问题。这不代表官网全部填写完成。</p>}
      {hasDraftEdits&&<div className="review-buttons"><button className="primary" disabled={!!busy||!canAnalyze||!plan||!readyCount} onClick={execute}>{busy==='filling'?<LoaderCircle className="spin" size={16}/>:<Save size={16}/>}保存答案并继续填写</button><button disabled={!!busy} onClick={discardDraft}>清除未保存修改</button></div>}
      {!hasDraftEdits&&plan&&<button className="secondary" onClick={()=>setWorkspaceSection('operation')}>回到投递操作</button>}
    </section>
    {memoryFailures.length>0&&<section className="card" role="alert"><h3>答案尚未记住</h3><p>输入仍保留。核对官网后可单独重试保存；记忆重试不会重新填写官网。</p>{memoryFailures.map(selector=>{const action=plan?.actions.find(item=>item.selector===selector);return action?<p key={selector}>{actionReviewTitle(action,snapshot?.fields.find(field=>field.selector===selector))}<button disabled={!!busy||stalePage} onClick={()=>remember(action)}>重试记住答案</button></p>:null})}</section>}
    {snapshot&&needsMonthPrecisionConsent(snapshot.url,plan)&&<section className="card"><h3>请确认日期填写方式</h3><p>简历只有年月、官网要求具体日期时，不会擅自补为1日。你可以提供精确日期，或确认以每月1日作为本网站的月份占位。</p><button disabled={Boolean(assistBlocker)} onClick={confirmMonthPrecision}>允许月份占位并记住</button></section>}
    {!canAnalyze&&hasDraftEdits&&<section className="card"><p>页面已变化，以下未保存答案仍保留，暂不执行。</p>{Object.entries(answers).map(([selector,value])=><p key={selector}>{snapshot?.fields.find(field=>field.selector===selector)?.question_text||'之前的表单项'}：{value}</p>)}</section>}
  </div>
  const diagnostics=<div className="stack">
    <section className="card"><h3>识别与执行检查</h3><p>此页供开发测试。识别缺口不要求用户补档案；隐藏诊断也不会把未完成的表单判为成功。</p>
      <div className="review-buttons">
        {!snapshot&&<button disabled={!!busy||!hasMatchingSafari||!backendReady} onClick={()=>void start()}>只连接并读取页面</button>}
        <button disabled={!!busy} onClick={restoreExistingSession}>只读同步当前页</button>
        {snapshot&&<button disabled={!!busy||!canAnalyze||hasDraftEdits} onClick={analyzeWithAI}>仅分析，不填写</button>}
        {snapshot&&<button disabled={!!busy||stalePage} onClick={async()=>{if(!snapshot||busy)return;setBusy('control-diagnostics');try{setControlDiagnostics(await api.recognitionDiagnostics(snapshot.session_id))}catch(error){notify(message(error))}finally{setBusy('')}}}>读取控件结构</button>}
      </div>
      {snapshot&&<p>页面：{snapshot.title} · {snapshot.fields.length} 个控件 · {workflow?workflowStageLabel(workflow.stage):'阶段未确认'}{stalePage?' · 旧计划暂停':''}</p>}
      {recoveryWarning&&<p role="alert">{recoveryWarning}</p>}
      {systemGroups.length>0&&<details open><summary>识别缺口（不是用户事实问题）</summary><ul>{systemGroups.map(group=><li key={group.key}><strong>{group.label}</strong>：{group.checkboxGroup?.blockReason||group.fields.map(userQuestionBlockReason).filter(Boolean).join('；')}<button disabled={!!busy||stalePage} onClick={()=>inspectField(group.fields[0])}>定位检查</button></li>)}</ul></details>}
    </section>
    {snapshot?.extraction_report&&<FormExtractionQuality report={snapshot.extraction_report}/>}
    {snapshot&&<ExtractionAudit snapshot={snapshot} resumeId={selectedResume} revision={plan?.context_token||''} disabled={!!busy||stalePage} onBusy={value=>setBusy(value?'extraction-audit':'')} notify={notify} onPageInvalidated={()=>{setObservedChange(workflow);setWorkflow(null);setCheck(null);setRecoveryWarning('招聘页面已变化或连接已失效。旧填写计划已暂停，临时答案保留，请先同步当前页。')}}/>}
    {snapshot&&<PageObservation snapshot={snapshot} resumeId={selectedResume} revision={plan?.context_token||''} disabled={!!busy||stalePage} onBusy={value=>setBusy(value?'page-observation':'')} notify={notify}/>}
    {controlDiagnostics&&<ControlDiagnostics data={controlDiagnostics}/>}
    {assistProgress&&<AssistProgress progress={assistProgress} onCancel={pauseAssist}/>}
    {assistResult&&<section className="card"><h3>{assistStatus(assistResult.status).title}</h3><p>{assistResult.message}</p><ol>{assistResult.events.map((event,index)=><li key={index}>{event.message}{Boolean(event.issues?.length)&&<ul>{event.issues?.map((issue,i)=><li key={i}>{issue.label}：{issue.message}</li>)}</ul>}</li>)}</ol>{Boolean(assistResult.record_coverage?.length)&&<ul>{assistResult.record_coverage?.map(row=><li key={row.kind}>{row.label}：{row.matched_records}/{row.source_total}；未覆盖：{row.missing_names.join('；')||'无'}</li>)}</ul>}</section>}
    {plan&&<section className="card"><h3>逐项核对</h3><ul>{visibleActions.map((action,index)=><li key={action.selector+'-'+index}><strong>{actionReviewTitle(action,snapshot?.fields.find(field=>field.selector===action.selector))}</strong> · {action.action} · {action.reason}</li>)}</ul></section>}
    {check&&<section className="card"><h3>提交前检查（不会自动提交）</h3><p>{overallReady?'允许人工终审':'仍未完成'} · 官网必填缺失 {check.required_missing.length} · 待模型 {pendingModelCount} · 未解决问题 {unresolvedGroups.length}</p><ul>{check.required_missing.map((item,index)=><li key={index}>{item.label||item.field_type}</li>)}{check.validation_errors.map((item,index)=><li key={'error'+index}>{item}</li>)}</ul>{execution&&<p>回读成功 {execution.verified} · 失败 {execution.failed}</p>}</section>}
    <details className="workbench-extra"><summary>维护投递知识</summary><ApplicationKnowledge snapshot={snapshot} profile={profile} plan={plan} disabled={!!busy||stalePage} onBusyChange={value=>setBusy(value?'knowledge-saving':'')} onChanged={refreshKnowledgePlan} notify={notify}/></details>
    {snapshot&&resumes.find(item=>item.id===selectedResume)&&<ResumeFactEditor key={selectedResume} resume={resumes.find(item=>item.id===selectedResume)!} disabled={!!busy} onBusy={value=>setBusy(value?'fact-saving':'')} onSaved={onFactSaved}/>}
  </div>
  return <ApplicationWorkspace stage={workspaceStage} section={workspaceSection} onSectionChange={setWorkspaceSection}
    resumes={resumes.map(resume=>({id:resume.id,label:resumeChoiceLabel(resume),disabled:['pending','parsing','failed'].includes(resume.status)}))}
    resumeId={selectedResume} onResumeChange={id=>void changeTaskResume(id)} url={url} onUrlChange={setUrl}
    onContinue={submitSetup} onBackToSetup={()=>{if(!busy)setWorkspaceStage('setup')}} disabled={!!busy}
    pendingCount={userPending.length} status={userStatus} applicationName={snapshot?.title||''}
    operation={operation} questions={questions} diagnostics={diagnostics}/>
}
const workflowStageLabel=(stage:ApplicationWorkflowState['stage'])=>({homepage:'招聘首页',job_list:'岗位列表',job_detail:'职位详情',registration_required:'创建账号',auth_required:'需要登录',verification_required:'等待验证',profile_form:'在线简历',application_form:'申请表单',review:'提交前复核',unknown:'待识别'}[stage])
const agentActionLabel=(action:ApplicationAgentTurn['decision']['next_action'])=>({browse_jobs:'浏览岗位入口',search_jobs:'搜索目标岗位',open_job:'打开指定岗位',start_application:'进入申请入口',analyze_and_fill:'分析、填写并回读',continue_application:'检查后进入下一页',refresh:'重新观察网页',wait_for_registration:'等待用户完成注册',wait_for_login:'等待用户登录',wait_for_verification:'等待用户验证',review_before_submit:'人工终审',stop:'暂停任务'}[action])
const recognitionProfileLabel=(profile:string)=>({"tencent-campus":'腾讯适配',"moka-campus":'Moka 适配',"beisen-italent":'北森适配',"generic-semantic":'通用语义识别'}[profile]||profile||'通用语义识别')
function WorkflowJourney({stage}:{stage:ApplicationWorkflowState['stage']}){const steps=['确认岗位','注册/登录','身份验证','填写申请','人工终审'];const index=['homepage','job_list','job_detail'].includes(stage)?0:stage==='registration_required'||stage==='auth_required'?1:stage==='verification_required'?2:stage==='profile_form'||stage==='application_form'?3:stage==='review'?4:0;return <div className="workflow-journey">{steps.map((label,i)=><div className={i<index?'done':i===index?'current':''} key={label}><span>{i<index?<Check size={12}/>:i+1}</span><small>{label}</small></div>)}</div>}
const comparisonStatusLabel=(status:FieldComparison['status'])=>({matched:'一致',missing:'网站漏填',conflict:'识别冲突',manual_review:'人工核对',unmapped:'缺少资料',option_unavailable:'选项不匹配'}[status])
const controlKind=(field:PageField)=>field.date_precision==='date'?'日期（年-月-日）':field.date_precision==='month'?'日期（年-月）':field.control_kind==='calendar'?'日历（格式待核实）':field.control_kind==='cascade'?'级联选择（需完整路径）':field.field_type==='section-button'?'需要先展开':field.field_type==='radio'?'单选题':field.field_type==='checkbox'?'勾选题':field.field_type==='select-multiple'||field.multiple?'下拉多选':field.field_type==='select-one'||field.field_type==='combobox'?'下拉单选':field.field_type==='textarea'?'长文本':'文本输入'
const fieldDisplayLabel=(field:PageField,fallback='')=>{const primary=field.question_text||field.group_label||field.label;const uncertain=fieldLabelUncertain(field);return uncertain&&fallback&&!/^(未识别|field|question)/i.test(fallback)?fallback:primary||fallback||field.context||field.placeholder||`未识别字段 ${field.ordinal||''}`.trim()}
const fieldContext=(field:PageField,label:string)=>{const parts=[field.context,field.help_text,...(field.nearby_labels||[]),...(field.section_path||[])].map(value=>(value||'').trim()).filter(value=>value&&value!==label);const context=[...new Set(parts)].slice(0,4).join(' · ');return context.length>360?`${context.slice(0,357)}…`:context}
const fieldLabelUncertain=(field:PageField)=>field.recognition_confidence<.7||['generated','name','placeholder','context','unknown'].includes(field.label_source||'unknown')

function SectionTitle({n,title,sub}:{n:string;title:string;sub:string}){return <div className="section-title"><span>{n}</span><div><h2>{title}</h2><p>{sub}</p></div></div>}
function Input({label,value,set,type='text'}:{label:string;value:string|number|null;set:(v:string)=>void;type?:string}){return <label className="field"><span>{label}</span><input type={type} value={value??''} onChange={e=>set(e.target.value)}/></label>}
function Select({label,value,values,set}:{label:string;value:string;values:string[];set:(v:string)=>void}){return <label className="field"><span>{label}</span><select value={value} onChange={e=>set(e.target.value)}>{values.map(v=><option key={v}>{v}</option>)}</select></label>}
function Text({label,value,set}:{label:string;value:string;set:(v:string)=>void}){return <label className="field text"><span>{label}</span><textarea value={value} onChange={e=>set(e.target.value)}/></label>}
function EditableList<T>({title,n,items,empty,set,render}:{title:string;n:string;items:T[];empty:T;set:(v:T[])=>void;render:(x:T,i:number,edit:(i:number,p:Partial<T>)=>void)=>React.ReactNode}){const edit=(i:number,p:Partial<T>)=>{const next=[...items];next[i]={...next[i],...p};set(next)};return <section className="card"><SectionTitle n={n} title={title} sub={`共 ${items.length} 条，可手动补充和修正`}/>{items.map((x,i)=><div className="list-item" key={i}><div className="list-head"><strong>{title} {i+1}</strong><button onClick={()=>set(items.filter((_,j)=>i!==j))}>移除</button></div>{render(x,i,edit)}</div>)}<button className="add" onClick={()=>set([...items,{...empty}])}><Plus size={16}/>添加一条</button></section>}
const split=(v:string)=>v.split(/[,，、]/).map(x=>x.trim()).filter(Boolean);const message=(e:unknown)=>e instanceof Error?e.message:'操作失败';const display=(v:unknown)=>typeof v==='string'?v:JSON.stringify(v);const formatBytes=(n:number)=>n?`${(n/1024).toFixed(n>1024*1024?0:1)} ${n>1024*1024?'MB':'KB'}`:'—'
