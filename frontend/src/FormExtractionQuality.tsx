import type {FormExtractionReport} from './types'

export default function FormExtractionQuality({report}:{report:FormExtractionReport}){
  return <section className="card extraction-quality" aria-label="网页信息提取质量">
    <h3>网页信息提取质量</h3>
    {report.capture_status!=='observed'&&<p>{report.capture_status==='partial'?'当前页面存在尚未覆盖的部分，不能判定整表完成。':'当前提取范围尚未核实。'}</p>}
    <p>当前读取 {report.question_count} 道题 · {report.verified_questions} 道原题有归属证据
      {report.unclear_questions>0&&` · ${report.unclear_questions} 道题干待核实`}
      {report.options_pending_questions>0&&` · ${report.options_pending_questions} 道选项或层级待补读`}</p>
    <small>仅针对当前已呈现页面，不代表整张申请表已经读全，也不代表已经填写。读取缺口不等于你缺少个人资料。</small>
    <details><summary>查看提取依据与尚未覆盖的部分</summary>
      <p>已识别控件 {report.captured_controls} 个；已排除登录、按钮等非填写控件 {report.intentionally_excluded_controls} 个；未映射控件 {report.unmapped_controls} 个。</p>
      {report.embedded_regions>0&&<p>未读取的嵌入区域：{report.embedded_regions} 个（可能包含表单或验证码）。</p>}
      {report.unread_shadow_regions>0&&<p>未读取的 Shadow DOM 控件区域：{report.unread_shadow_regions} 个。</p>}
      {report.pending_sections.length>0&&<p>尚未展开或未激活的栏目：{report.pending_sections.join('、')}。</p>}
      <ul>{report.limitations.map(item=><li key={item}>{item}</li>)}</ul>
      {report.issues.length>0&&<><h4>需要补读或核实的信息</h4><ul>{report.issues.slice(0,15).map((issue,index)=><li key={index}><strong>{issue.label}</strong>：{issue.reason}</li>)}</ul>
        {report.issues.length>15&&<small>另有 {report.issues.length-15} 项，逐题的识别依据中可查看详情。</small>}</>}
    </details>
  </section>
}
