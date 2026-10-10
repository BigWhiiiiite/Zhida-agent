import { useId, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { ArrowLeft, ArrowRight, ChevronDown, FileText, ListChecks, Settings2 } from 'lucide-react'
import './application-workspace.css'

export type ApplicationWorkspaceSection = 'operation' | 'questions' | 'diagnostics'
export type ApplicationWorkspaceResume = { id: string; label: string; disabled?: boolean }
export type ApplicationWorkspaceSelection = { resumeId: string; url: string }

export type ApplicationWorkspaceProps = {
  stage: 'setup' | 'workspace'
  section: ApplicationWorkspaceSection
  onSectionChange: (section: ApplicationWorkspaceSection) => void
  resumes: ApplicationWorkspaceResume[]
  resumeId: string
  onResumeChange: (id: string) => void
  url: string
  onUrlChange: (url: string) => void
  onContinue: (selection: ApplicationWorkspaceSelection) => void
  onBackToSetup: () => void
  disabled?: boolean
  pendingCount?: number
  status?: string
  applicationName?: string
  operation: ReactNode
  questions?: ReactNode
  diagnostics?: ReactNode
}

export function validateApplicationSetup(resumes: ApplicationWorkspaceResume[], resumeId: string, rawUrl: string) {
  const selectedResume = resumes.find(resume => resume.id === resumeId && !resume.disabled)
  const resumeError = selectedResume ? '' : resumes.some(resume => !resume.disabled)
    ? '请选择一份可用的简历。' : '请先在简历资料库添加一份可用的简历。'
  const url = rawUrl.trim()
  let urlError = ''
  if (!url) urlError = '请输入信息填写页的网址。'
  else {
    try {
      const parsed = new URL(url)
      if (!['http:', 'https:'].includes(parsed.protocol) || !parsed.hostname || parsed.username || parsed.password) {
        urlError = '请使用以 http:// 或 https:// 开头的信息填写页网址。'
      }
    } catch {
      urlError = '请输入完整的网址，例如 https://example.com/apply。'
    }
  }
  return { valid: !resumeError && !urlError, resumeError, urlError, selection: { resumeId, url } }
}

const sections = [
  { id: 'operation', label: '投递操作', Icon: FileText },
  { id: 'questions', label: '待补充资料', Icon: ListChecks },
  { id: 'diagnostics', label: '开发检查', Icon: Settings2 },
] as const

export default function ApplicationWorkspace(props: ApplicationWorkspaceProps) {
  const { stage, section, resumes, resumeId, url, disabled = false, pendingCount = 0 } = props
  const id = useId()
  const [urlTouched, setUrlTouched] = useState(false)
  const [resumeTouched, setResumeTouched] = useState(false)
  const validation = validateApplicationSetup(resumes, resumeId, url)
  const count = Number.isFinite(pendingCount) ? Math.max(0, Math.floor(pendingCount)) : 0
  const selectedResume = resumes.find(resume => resume.id === resumeId)
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setResumeTouched(true)
    setUrlTouched(true)
    if (!disabled && validation.valid) props.onContinue(validation.selection)
  }

  if (stage === 'setup') return <section className="application-workspace application-setup" aria-labelledby={`${id}-setup-title`}>
    <div className="application-setup-heading">
      <h2 id={`${id}-setup-title`}>准备本次投递</h2>
      <p id={`${id}-setup-help`}>网址必须是信息填写页，登录和选岗已由用户完成。</p>
    </div>
    <form onSubmit={submit} noValidate aria-describedby={`${id}-setup-help`}>
      <div className="application-setup-field">
        <label htmlFor={`${id}-resume`}>投递简历</label>
        <select id={`${id}-resume`} value={resumeId} disabled={disabled || !resumes.length}
          aria-invalid={Boolean(validation.resumeError && (resumeTouched || !resumes.some(resume => !resume.disabled)))}
          aria-describedby={`${id}-resume-help`}
          onBlur={() => setResumeTouched(true)} onChange={event => { setResumeTouched(true); props.onResumeChange(event.target.value) }}>
          <option value="">选择简历</option>
          {resumes.map(resume => <option key={resume.id} value={resume.id} disabled={resume.disabled}>{resume.label}</option>)}
        </select>
        <p id={`${id}-resume-help`} className={validation.resumeError && (resumeTouched || !resumes.some(resume => !resume.disabled)) ? 'application-field-error' : 'application-field-help'}>
          {validation.resumeError && (resumeTouched || !resumes.some(resume => !resume.disabled)) ? validation.resumeError : '本次填写将使用这份简历。'}
        </p>
      </div>
      <div className="application-setup-field">
        <label htmlFor={`${id}-url`}>信息填写页网址</label>
        <input id={`${id}-url`} type="url" inputMode="url" autoComplete="url" spellCheck={false}
          value={url} disabled={disabled} placeholder="https://…" required
          aria-invalid={Boolean(urlTouched && validation.urlError)} aria-describedby={`${id}-url-help`}
          onBlur={() => setUrlTouched(true)} onChange={event => props.onUrlChange(event.target.value)} />
        <p id={`${id}-url-help`} className={urlTouched && validation.urlError ? 'application-field-error' : 'application-field-help'}>
          {urlTouched && validation.urlError ? validation.urlError : '复制招聘网站中当前信息填写页的完整网址。'}
        </p>
      </div>
      <button className="application-continue" type="submit" disabled={disabled || !validation.valid}>
        {disabled ? '请稍候…' : '确认并继续'}<ArrowRight size={16} aria-hidden="true" />
      </button>
    </form>
  </section>

  const selectedSection = sections.find(item => item.id === section)!
  const contents = {
    operation: props.operation,
    questions: props.questions ?? <p className="application-section-empty">目前没有需要补充的资料。</p>,
    diagnostics: props.diagnostics ?? <p className="application-section-empty">开始投递后，可在这里查看开发检查记录。</p>,
  }
  return <section className="application-workspace application-session" aria-label="本次投递工作台">
    <div className="application-session-menu">
      <details className="application-menu" open>
        <summary>本次投递<ChevronDown size={15} aria-hidden="true" /></summary>
        <nav aria-label="投递工作台页面">
          {sections.map(({ id: key, label, Icon }) => <button key={key} type="button"
            className={section === key ? 'selected' : ''} aria-current={section === key ? 'page' : undefined}
            aria-controls={`${id}-content`} onClick={() => props.onSectionChange(key)}>
            <Icon size={16} aria-hidden="true" /><span>{label}</span>
            {key === 'questions' && count > 0 && <b aria-label={`${count} 项待补充`}>{count}</b>}
          </button>)}
        </nav>
      </details>
      <div className="application-session-context">
        <span>使用简历</span><strong>{selectedResume?.label || '尚未选择'}</strong>
        {props.applicationName && <small>{props.applicationName}</small>}
      </div>
      <button className="application-back" type="button" disabled={disabled} onClick={props.onBackToSetup}
        title="返回修改简历和网址，招聘官网页面会保留">
        <ArrowLeft size={15} aria-hidden="true" />返回设置
      </button>
      <small className="application-back-note">招聘官网页面会保留</small>
    </div>
    <div id={`${id}-content`} className="application-session-content" aria-labelledby={`${id}-section-title`}>
      <div className="application-content-heading">
        <h2 id={`${id}-section-title`}>{selectedSection.label}</h2>
        {props.status && <p role="status" aria-live="polite">{props.status}</p>}
      </div>
      {sections.map(({ id: key }) => <div key={key} data-workspace-section={key}
        className={`application-section-body application-section-${key}`} hidden={section !== key}>
        {contents[key]}
      </div>)}
    </div>
  </section>
}
