import {ExternalLink} from 'lucide-react'
import {inspectTaskUrl} from './chatTask'
import './browser-opening.css'

// A user-clicked, ordinary navigation. No backend, model, browser driver or
// automation permission is required, and no existing application is changed.
export function RecruitingWebsiteLink({url,notify}:{url:string;notify:(message:string)=>void}) {
  const destination=inspectTaskUrl(url)
  return <a className="primary direct-website-link" href={destination.error?undefined:destination.url}
    target="_blank" rel="noopener noreferrer" aria-disabled={Boolean(destination.error)}
    onClick={event=>{
      if(destination.error){event.preventDefault();notify(destination.error);return}
      notify('已请求在当前浏览器打开。若进入 VS Code 内置浏览器，请改用上方 Safari／Chrome 按钮。尚未连接自动填写。')
    }}><ExternalLink size={17}/>直接打开招聘网站</a>
}
