import {useState} from 'react'
import {Copy,ExternalLink,LoaderCircle} from 'lucide-react'
import {inspectTaskUrl} from './chatTask'
import {RecruitingWebsiteLink} from './RecruitingWebsiteLink'
import {requestExternalWebsite} from './normalWebsiteNavigation'
import type {ExternalOpener,NormalBrowser,OpenedWebsite} from './normalWebsiteNavigation'

export function ExternalWebsiteOpening({url,notify,openExternal,onOpened,connected=false}:{url:string;notify:(message:string)=>void;openExternal:ExternalOpener;onOpened?:(result:OpenedWebsite,url:string)=>void;connected?:boolean}) {
  const [browser,setBrowser]=useState<NormalBrowser>('safari')
  const [opening,setOpening]=useState(false)
  const destination=inspectTaskUrl(url)
  const open=async()=>{
    if(opening||connected)return
    setOpening(true)
    try{await requestExternalWebsite(url,browser,openExternal,notify,onOpened)}finally{setOpening(false)}
  }
  const copy=async()=>{
    if(destination.error){notify(destination.error);return}
    try{await navigator.clipboard.writeText(destination.url);notify('网址已复制，请粘贴到 Safari 或 Chrome 地址栏。')}
    catch{notify('复制未完成，请在左侧网址输入框选中并手动复制。')}
  }
  return <div className="normal-browser-opening">
    <label className="normal-browser-choice"><span>在本机浏览器打开</span><select aria-label="选择打开招聘网站的浏览器" disabled={opening} value={browser} onChange={event=>setBrowser(event.target.value as NormalBrowser)}><option value="safari">Safari（推荐）</option><option value="chrome">Google Chrome</option></select></label>
    <button className="primary" disabled={connected||opening||Boolean(destination.error)} onClick={open}>{opening?<LoaderCircle className="spin" size={17}/>:<ExternalLink size={17}/>}用 {browser==='safari'?'Safari':'Chrome'} 打开招聘网站</button>
    <button className="secondary" disabled={Boolean(destination.error)} onClick={copy}><Copy size={15}/>复制网址</button>
    <details className="ordinary-link-fallback"><summary>备用：在当前浏览器打开</summary><RecruitingWebsiteLink url={url} notify={notify}/><small>在 VS Code 中使用时，这个备用链接可能仍进入内置浏览器。请优先使用上面的 Safari／Chrome 按钮。</small></details>
  </div>
}
