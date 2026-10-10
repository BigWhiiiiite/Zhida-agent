type Shape = {tag?:string;classes?:string[];role?:string;captions?:string[]}
type Control = Shape & {ordinal?:number;type?:string;readonly?:boolean;ancestors?:Shape[]}

/** Public widget structure only; no values, credentials or serialized HTML. */
export default function ControlDiagnostics({data}:{data:Record<string,unknown>}){
  const controls=(Array.isArray(data.controls)?data.controls:[]) as Control[]
  return <details open><summary>本轮结构证据 · {controls.length} 个控件{data.truncated?'（已截断）':''}</summary>
    <ol style={{maxHeight:420,overflow:'auto'}}>{controls.map((control,index)=>{
      const ancestors=control.ancestors||[]
      const question=ancestors.find(parent=>parent.captions?.length)?.captions?.join(' / ')||'题目需核对'
      const wrappers=ancestors.slice(0,10).map(parent=>parent.classes?.join('.')).filter(Boolean).join(' → ')
      return <li key={index}><p>{control.ordinal}. {question}</p><small>{control.tag} / {control.type||control.role||'容器'}{control.readonly?' · 只读':''} · {control.classes?.join('.')||'无类名'} · {wrappers}</small></li>
    })}</ol>
    <details><summary>完整结构 JSON（不含填写值）</summary><pre style={{whiteSpace:'pre-wrap',maxHeight:420,overflow:'auto'}}>{JSON.stringify(data,null,2)}</pre></details>
  </details>
}
