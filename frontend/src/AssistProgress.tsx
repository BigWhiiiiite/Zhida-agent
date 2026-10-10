import type {ApplicationAssistProgress} from './types'

const stages=[['observe','读取网页'],['retrieve','检索资料与记忆'],['decide','判断与分析'],['execute','填写'],['verify','回读核对']] as const
export default function AssistProgress({progress,onCancel}:{progress:ApplicationAssistProgress;onCancel:()=>void}){
  const current=progress.phase==='model'?'decide':progress.phase==='fill'?'verify':progress.phase==='control'?'execute':progress.phase
  const verified=progress.events.reduce((n,event)=>n+event.completed,0)
  const failed=progress.events.reduce((n,event)=>n+event.failed,0)
  const issues=progress.events.flatMap(event=>event.issues??[])
    .filter((issue,index,all)=>all.findIndex(other=>other.label===issue.label&&other.message===issue.message)===index)
  return <section className="card assist-live" aria-label="职达填写闭环进度">
    <h3>{progress.status==='interrupted'?'职达已暂停本轮填写':progress.status==='finished'?'本轮处理已结束':'职达正在填写并核对'}</h3>
    <ol className="assist-stages">{stages.map(([key,label])=><li key={key} aria-current={key===current?'step':undefined}>{label}</li>)}</ol>
    <p role="status" aria-live="polite">{progress.message}</p>
    <small>本轮回读成功 {verified} 次 · 失败 {failed} 次 · 不代表全部表单已完成 · 未提交</small>
    {issues.length>0&&<section aria-label="本轮未通过核验的字段">
      <h4>以下字段未通过核验或仍需重新核对</h4>
      <ul>{issues.map((issue,index)=><li key={index}><strong>{issue.label||'未能确认题目的字段'}</strong>：{issue.message}</li>)}</ul>
      <small>失败不代表资料不存在；请按原因核对，不重复点击填写。</small>
    </section>}
    {progress.status==='running'&&<button className="secondary" disabled={progress.cancel_requested} onClick={onCancel}>{progress.cancel_requested?'已请求暂停，等待当前操作结束':'暂停本轮填写'}</button>}
    <details><summary>查看本轮过程</summary><ol>{progress.events.map((event,index)=><li key={index}>{event.message}</li>)}</ol></details>
  </section>
}
