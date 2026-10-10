import {useEffect,useRef,useState} from 'react'
import {LoaderCircle,Link as LinkIcon} from 'lucide-react'
import {api} from './api'
import {inspectTaskUrl} from './chatTask'
import type {OpenedWebsite} from './normalWebsiteNavigation'

export function ExistingSafariConnection({url,disabled,onBound,onBusyChange,notify}:{
  url:string;disabled:boolean;onBound:(result:OpenedWebsite,url:string)=>void;
  onBusyChange:(busy:boolean)=>void;notify:(message:string)=>void;
}) {
  const [choosing,setChoosing]=useState(false)
  const [remaining,setRemaining]=useState(10)
  const [preview,setPreview]=useState<{preview_token:string;url:string}|null>(null)
  const request=useRef(0)
  const destination=inspectTaskUrl(url)
  useEffect(()=>{request.current++;setPreview(null)},[url])
  useEffect(()=>()=>{request.current++},[])
  useEffect(()=>{
    if(!choosing)return
    const timer=window.setInterval(()=>setRemaining(value=>Math.max(0,value-1)),1000)
    return()=>window.clearInterval(timer)
  },[choosing])

  const choose=async()=>{
    if(disabled||choosing||destination.error)return
    if(!window.confirm(`允许职达读取你置前的一个 Safari 标签页地址，并连接 ${destination.hostname} 的这一个填写页吗？不会扫描其他标签页，也不会填写或提交。点击确定后，请在10秒内把招聘填写窗口置前。`))return
    const ticket=++request.current
    setPreview(null);setRemaining(10);setChoosing(true);onBusyChange(true)
    notify('请在10秒内把目标招聘填写页置前；只读取这一标签页的地址，不扫描其他标签页。')
    try {
      const data=await api.previewExistingSafari(destination.url)
      if(ticket!==request.current)return
      if(data.url!==destination.url)throw new Error('窗口地址与填写链接不一致，未连接')
      setPreview(data)
      notify('已核对你指定的标签页地址。请回职达点击“确认连接这一页”；尚未分析或填写。')
    }catch(error){if(ticket===request.current)notify(error instanceof Error?error.message:'窗口检查失败，未连接')}
    finally{if(ticket===request.current){setChoosing(false);onBusyChange(false)}}
  }
  const confirm=async()=>{
    if(!preview||disabled||choosing)return
    const ticket=++request.current
    onBusyChange(true)
    try {
      const result=await api.confirmExistingSafari(preview.url,preview.preview_token)
      if(ticket!==request.current)return
      onBound(result,preview.url);setPreview(null)
      notify('已连接你指定的 Safari 填写页，没有重开或刷新。现在可以点击“投递（辅助填写）”。')
    }catch(error){if(ticket===request.current){setPreview(null);notify(error instanceof Error?error.message:'窗口确认失败，未连接')}}
    finally{if(ticket===request.current)onBusyChange(false)}
  }
  return <div className="existing-safari-connection">
    <button className="secondary" disabled={disabled||choosing||Boolean(destination.error)} onClick={choose}>
      {choosing?<LoaderCircle className="spin" size={16}/>:<LinkIcon size={16}/>}
      {choosing?`请置前招聘窗口 · ${remaining>0?`${remaining}秒`:'核对中'}`:'连接我已登录的 Safari 窗口'}
    </button>
    <small>已有登录页、旧连接丢失时使用。先由你指定窗口并核对地址，再连接；不会另开窗口。</small>
    {preview&&<div className="existing-safari-preview"><p>只连接这个标签页：{preview.url}</p>
      <button className="secondary" disabled={disabled} onClick={confirm}>确认连接这一页</button>
      <button className="secondary" disabled={disabled} onClick={()=>{request.current++;setPreview(null)}}>取消</button>
    </div>}
  </div>
}
