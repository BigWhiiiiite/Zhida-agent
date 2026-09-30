import { useEffect, useMemo, useState } from 'react'
import { BookOpen, Check, Link2, LoaderCircle, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import { api } from './api'
import type { ApplicationKnowledgeRecord, BrowserSnapshot, CandidateProfile, FormPlan, KnowledgeMappingTarget, PageField } from './types'
import { mappingPreview } from './applicationKnowledgePreview'
import './application-knowledge.css'

const educationScopes = [
  ['education:high_school', '高中'], ['education:associate', '专科'],
  ['education:bachelor', '本科'], ['education:master', '硕士'], ['education:doctorate', '博士'],
] as const
const scopeLabel = (value = '') => educationScopes.find(([key]) => key === value)?.[1] || value
const questionTitle = (field:PageField) => field.question_text || field.group_label || field.label || `未识别问题 ${field.ordinal}`
const fieldSection = (field:PageField) => field.section || field.section_path.join(' > ')
const uncertainTitle = (field:PageField) => field.recognition_confidence<.7 || ['generated','name','placeholder','context','unknown'].includes(field.label_source || 'unknown') || /^(是|否|男|女|yes|no|未识别.*|\d+)$/i.test(questionTitle(field).trim())
const knowledgeScopeLabel = (record:ApplicationKnowledgeRecord) => {
  let host='招聘网站'
  try {host=new URL(record.source_url).hostname} catch {/* Keep a readable fallback for legacy records. */}
  const range=record.site_scope.startsWith('page:')?'仅本页':'同公司'
  return [host,record.section,range].filter(Boolean).join(' · ')
}
const errorMessage = (error:unknown) => error instanceof Error ? error.message : '操作失败，请重试'

type Props = {
  snapshot:BrowserSnapshot|null;
  profile:CandidateProfile|null;
  plan:FormPlan|null;
  disabled:boolean;
  onBusyChange:(busy:boolean)=>void;
  onChanged:(selector?:string)=>Promise<void>;
  notify:(message:string)=>void;
}

export default function ApplicationKnowledge({snapshot, profile, plan, disabled, onBusyChange, onChanged, notify}:Props) {
  const [records,setRecords] = useState<ApplicationKnowledgeRecord[]>([])
  const [targets,setTargets] = useState<KnowledgeMappingTarget[]>([])
  const [loading,setLoading] = useState(true)
  const [loadError,setLoadError] = useState('')
  const [busy,setBusy] = useState(false)
  const [showAll,setShowAll] = useState(false)
  const [selected,setSelected] = useState<PageField|null>(null)
  const [question,setQuestion] = useState('')
  const [section,setSection] = useState('')
  const [aliases,setAliases] = useState('')
  const [targetPath,setTargetPath] = useState('')
  const [entityScope,setEntityScope] = useState('')
  const [note,setNote] = useState('')
  const [confirmed,setConfirmed] = useState(false)
  const [ruleOpen,setRuleOpen] = useState(false)
  const [ruleTitle,setRuleTitle] = useState('')
  const [ruleNote,setRuleNote] = useState('')
  const [ruleExpiry,setRuleExpiry] = useState('')
  const [ruleConfirmed,setRuleConfirmed] = useState(false)
  const [formError,setFormError] = useState('')
  const blocked = disabled || busy

  useEffect(() => {
    let active = true
    Promise.all([api.applicationKnowledge(),api.applicationKnowledgeTargets()])
      .then(([items,fields]) => { if (active) {setRecords(items);setTargets(fields);setLoadError('')} })
      .catch(error => {if (active) setLoadError(errorMessage(error))})
      .finally(() => {if (active) setLoading(false)})
    return () => {active=false}
  }, [])

  useEffect(() => {setSelected(null);setRuleOpen(false);setConfirmed(false);setRuleConfirmed(false);setFormError('')}, [snapshot?.session_id,snapshot?.url])

  const reload = async () => {
    setLoading(true);setLoadError('')
    try {const [items,fields]=await Promise.all([api.applicationKnowledge(),api.applicationKnowledgeTargets()]);setRecords(items);setTargets(fields)}
    catch(error) {setLoadError(errorMessage(error))}
    finally {setLoading(false)}
  }
  const candidateFields = useMemo(() => {
    const unresolved = new Set(plan?.actions.filter(action=>action.action==='ask_user').map(action=>action.selector))
    const seen = new Set<string>()
    return (snapshot?.fields || []).filter(field => {
      if (['file','password','hidden','section-button','submit','button'].includes(field.field_type)) return false
      if (!showAll && plan && !unresolved.has(field.selector)) return false
      const groupKey = field.field_type==='radio' ? field.control_group_key || field.group_label || field.selector : field.selector
      if (seen.has(groupKey)) return false
      seen.add(groupKey)
      return true
    })
  }, [snapshot,plan,showAll])
  const target = targets.find(item=>item.path===targetPath)
  const preview = mappingPreview(profile,target,entityScope)
  const matches = plan?.knowledge_matches || []

  const chooseField = (field:PageField) => {
    setSelected(field);setQuestion(questionTitle(field));setSection(fieldSection(field))
    setAliases('');setNote('');setConfirmed(false);setFormError('');setRuleOpen(false)
    const inferred = targets.find(item=>item.semantic_key===field.semantic_key)
    setTargetPath(inferred?.path || '')
    setEntityScope(educationScopes.some(([scope])=>scope===field.entity_scope) ? field.entity_scope : '')
  }
  const saveMapping = async () => {
    if (!snapshot || !selected || !target || !confirmed || !question.trim() || target.requires_entity_scope && !entityScope) return
    const aliasList=aliases.split('\n').map(item=>item.trim()).filter(Boolean)
    if(aliasList.length>12||aliasList.some(item=>item.length>500)){setFormError('同义问法最多 12 条，每条不超过 500 字。');return}
    setBusy(true);onBusyChange(true);setFormError('')
    let saved=false
    try {
      await api.saveApplicationKnowledge({kind:'mapping',question:question.trim(),aliases:aliasList,source_url:snapshot.url,section,profile_path:targetPath,entity_scope:target.requires_entity_scope?entityScope:'',field_signature:selected.field_signature,field_type:selected.field_type,note,confirmed:true})
      saved=true
      setRecords(await api.applicationKnowledge());setSelected(null);setConfirmed(false)
      await onChanged(selected.selector)
      notify('对照已保存并重新核对。只记住字段含义，不修改主档案或自动填写网页；该字段旧的临时答案已清除。')
    } catch(error) {setFormError(`${saved?'对照已经保存，但列表刷新或重新核对失败，请点击“立即核对”：':''}${errorMessage(error)}`)}
    finally {setBusy(false);onBusyChange(false)}
  }
  const saveRule = async () => {
    if (!snapshot || !ruleTitle.trim() || !ruleNote.trim() || !ruleConfirmed) return
    setBusy(true);onBusyChange(true);setFormError('')
    let saved=false
    try {
      await api.saveApplicationKnowledge({kind:'rule',question:ruleTitle.trim(),source_url:snapshot.url,note:ruleNote.trim(),confirmed:true,expires_at:ruleExpiry ? new Date(`${ruleExpiry}T23:59:59`).toISOString() : null})
      saved=true
      setRecords(await api.applicationKnowledge());setRuleOpen(false);setRuleTitle('');setRuleNote('');setRuleExpiry('');setRuleConfirmed(false)
      await onChanged()
      notify('规则已保存为本站参考资料。规则不会自动执行操作，也不会替你同意承诺或提交申请。')
    } catch(error) {setFormError(`${saved?'规则已经保存，但列表刷新或重新核对失败，请点击“立即核对”：':''}${errorMessage(error)}`)}
    finally {setBusy(false);onBusyChange(false)}
  }
  const remove = async (record:ApplicationKnowledgeRecord) => {
    if (!window.confirm(`删除“${record.question}”的${record.kind==='mapping'?'字段对照':'参考规则'}？主档案和网页已填写内容不会被修改。`)) return
    setBusy(true);onBusyChange(true);setFormError('')
    let deleted=false
    try {await api.deleteApplicationKnowledge(record.id);deleted=true;setRecords(items=>items.filter(item=>item.id!==record.id));await onChanged();notify('已删除这条知识并重新核对，主档案与网页内容未修改。')}
    catch(error) {setFormError(`${deleted?'知识已删除，但重新核对失败，请点击“立即核对”：':''}${errorMessage(error)}`)}
    finally {setBusy(false);onBusyChange(false)}
  }

  return <section className="card application-knowledge">
    <div className="knowledge-head"><div><BookOpen size={20}/><div><h2>投递知识库 · 教会 Agent 一次</h2><p>把网页原题对照到主档案。下次按当前公司、题目和学历层级检索，取最新资料，不硬背旧答案。</p></div></div><span>仅你的账号 · {records.length} 条</span></div>
    {loadError && <div role="alert" className="knowledge-warning">知识库暂时无法读取：{loadError}<button type="button" disabled={loading||blocked} onClick={reload}><RefreshCw size={13}/>重试</button></div>}
    {formError && <p role="alert" className="knowledge-warning">{formError}</p>}
    {matches.length>0 && <details className="knowledge-matches"><summary>本页检索到 {matches.length} 条知识依据</summary>{matches.map((match,index)=><div key={`${match.selector}-${match.knowledge_id}-${index}`}><strong>{match.question}</strong><span className={match.usable?'usable':'review'}>{match.usable?'已通过复用检查':'仅供核对'}</span><small>{match.profile_path ? `${targets.find(item=>item.path===match.profile_path)?.label || match.profile_path}${match.entity_scope?` · ${scopeLabel(match.entity_scope)}`:''} · ` : ''}{match.reason}</small></div>)}</details>}
    {!snapshot ? <p className="knowledge-empty">先打开招聘网页，再把该网站的问题与主档案建立对照。所有公司使用同一套机制。</p> : <details className="knowledge-field-picker">
      <summary>识别不清或对应错误？手动建立字段对照</summary>
      <div className="knowledge-picker-tools"><p>先在招聘网页核对完整题目，不能把单个“是 / 否”当作问题。</p><label><input type="checkbox" checked={showAll} onChange={event=>setShowAll(event.target.checked)}/>显示全部可填写字段</label></div>
      <div className="knowledge-field-list">{candidateFields.length ? candidateFields.map(field=><div key={field.selector}><span><strong>{questionTitle(field)}</strong><small>{fieldSection(field) || '未识别分区'} · {field.field_type}{field.options.length?` · 选项：${field.options.slice(0,5).join('、')}`:''}</small></span><button type="button" disabled={blocked||loading||!targets.length} onClick={()=>chooseField(field)}><Link2 size={13}/>建立对照</button></div>) : <p className="knowledge-empty">暂无待确认字段；可以勾选“显示全部可填写字段”纠正已有识别。</p>}</div>
    </details>}
    {selected && <div className="knowledge-editor" role="region" aria-label="建立字段对照">
      <div className="knowledge-editor-title"><h3>网页问题 → 主档案字段</h3><button type="button" aria-label="关闭字段对照" disabled={blocked} onClick={()=>setSelected(null)}><X size={16}/></button></div>
      <p className="knowledge-source">当前网页：{snapshot?.title || snapshot?.url}<br/>识别上下文：{[selected.context,selected.help_text].filter(Boolean).join(' · ') || '未读取到上下文，请先去招聘网页核对。'}</p>
      {!fieldSection(selected)&&uncertainTitle(selected)&&<p className="knowledge-warning">这个问题的标题不明确，且网页缺乏稳定分区。你可以保存人工对照，但校正关系可能仍需人工核对；不会手写一个分区来绕过校验。</p>}
      <div className="knowledge-form-grid">
        <label className="field"><span>网页完整问题（请纠正含糊标题）</span><input value={question} disabled={blocked} onChange={event=>{setQuestion(event.target.value);setConfirmed(false)}} maxLength={500}/></label>
        <label className="field"><span>所在分区（保留网页识别值）</span><input value={section} readOnly placeholder="网页未提供稳定分区"/></label>
        <label className="field"><span>对应主档案哪一项</span><select value={targetPath} disabled={blocked} onChange={event=>{setTargetPath(event.target.value);setConfirmed(false)}}><option value="">请选择，不确定请不要保存</option>{targets.map(item=><option value={item.path} key={item.path}>{item.label}</option>)}</select></label>
        {target?.requires_entity_scope && <label className="field"><span>必须明确学历层级</span><select value={entityScope} disabled={blocked} onChange={event=>{setEntityScope(event.target.value);setConfirmed(false)}}><option value="">请选择实际对应的学历</option>{educationScopes.map(([value,label])=><option value={value} key={value}>{label}</option>)}</select></label>}
      </div>
      <div className={`knowledge-preview ${preview.value?'has-value':''}`}><small>主档案当前值（不会修改）</small><strong>{preview.value || '尚无可安全使用的值'}</strong><p>{preview.message}</p></div>
      <details className="knowledge-extra"><summary>补充已核实的同义问法或备注（可选）</summary><label className="field"><span>同一问题的其他问法，每行一条</span><textarea value={aliases} disabled={blocked} onChange={event=>{setAliases(event.target.value);setConfirmed(false)}} placeholder="只添加含义完全一致的问法，不要填写答案" maxLength={2000}/></label><label className="field"><span>核对依据</span><textarea value={note} disabled={blocked} onChange={event=>{setNote(event.target.value);setConfirmed(false)}} placeholder="例如：已核对该题位于硕士教育经历区域" maxLength={2000}/></label></details>
      <label className="knowledge-confirm"><input type="checkbox" checked={confirmed} disabled={blocked} onChange={event=>setConfirmed(event.target.checked)}/>我已核对网页原题与主档案对应关系；这不是替本人承诺或填写他人资料。</label>
      <div className="knowledge-save"><small>保存后重新核对，清除本字段旧的临时答案；不会自动操作招聘网站。</small><button type="button" className="primary" disabled={blocked||!confirmed||!target||!question.trim()||!!target?.requires_entity_scope&&!entityScope} onClick={saveMapping}>{busy?<LoaderCircle className="spin" size={15}/>:<Check size={15}/>}确认对照并重新核对</button></div>
    </div>}
    <div className="knowledge-rule-toolbar"><span>也可以记录本公司的投递限制、材料格式等规则，作为模型分析的参考。</span><button type="button" disabled={!snapshot||blocked} onClick={()=>{setRuleOpen(value=>!value);setSelected(null);setFormError('')}}><Plus size={14}/>{ruleOpen?'收起规则':'添加本站规则'}</button></div>
    {ruleOpen && <div className="knowledge-editor"><h3>添加已核实的公司规则</h3><div className="knowledge-form-grid"><label className="field"><span>规则标题 / 对应问题</span><input value={ruleTitle} disabled={blocked} onChange={event=>{setRuleTitle(event.target.value);setRuleConfirmed(false)}} placeholder="例如：简历附件格式要求" maxLength={500}/></label><label className="field"><span>有效截止日期（可选）</span><input type="date" value={ruleExpiry} disabled={blocked} onChange={event=>{setRuleExpiry(event.target.value);setRuleConfirmed(false)}}/></label></div><label className="field"><span>网页规则原文与适用范围</span><textarea value={ruleNote} disabled={blocked} onChange={event=>{setRuleNote(event.target.value);setRuleConfirmed(false)}} placeholder="请记录网页明确写出的要求，不要添加密码、验证码或他人的个人资料。" maxLength={2000}/></label><label className="knowledge-confirm"><input type="checkbox" checked={ruleConfirmed} disabled={blocked} onChange={event=>setRuleConfirmed(event.target.checked)}/>我已核对当前公司网页；这条资料仅供参考，不授权自动执行额外操作。</label><button type="button" className="primary" disabled={blocked||!ruleConfirmed||!ruleTitle.trim()||!ruleNote.trim()} onClick={saveRule}>保存参考规则</button></div>}
    <details className="knowledge-records"><summary>管理我的投递知识（{records.length}）{loading?' · 正在读取':''}</summary>{records.length ? records.map(record=><article key={record.id}><div><strong>{record.question}</strong><span>{record.kind==='mapping'?`字段对照 → ${targets.find(item=>item.path===record.profile_path)?.label || record.profile_path}${record.entity_scope?` · ${scopeLabel(record.entity_scope)}`:''}`:'公司参考规则'}</span><small>适用范围：{knowledgeScopeLabel(record)} · {record.updated_at?new Date(record.updated_at).toLocaleDateString():''}{record.expires_at?` · ${new Date(record.expires_at).getTime()<Date.now()?'已过期':'有效至'} ${new Date(record.expires_at).toLocaleDateString()}`:''}</small>{record.note&&<p>{record.note}</p>}</div><button type="button" disabled={blocked} aria-label={`删除${record.question}`} onClick={()=>remove(record)}><Trash2 size={14}/>删除</button></article>) : <p className="knowledge-empty">尚未保存对照或规则。知识只归当前账号，不会自动共享给其他用户。</p>}</details>
  </section>
}
