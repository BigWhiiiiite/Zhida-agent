import {inspectTaskUrl} from './chatTask'

// Shared request contract for ordinary native-app navigation (not autofill).
export type NormalBrowser='safari'|'chrome'
export type OpenedWebsite={status:string;browser:NormalBrowser;automation_connected:boolean;safari_window_token?:string;window_reused?:boolean}
export type ExternalOpener=(url:string,browser:NormalBrowser)=>Promise<OpenedWebsite>

export async function requestExternalWebsite(url:string,browser:NormalBrowser,open:ExternalOpener,notify:(message:string)=>void,onOpened?:(result:OpenedWebsite,url:string)=>void) {
  const destination=inspectTaskUrl(url)
  if(destination.error){notify(destination.error);return false}
  try {
    const result=await open(destination.url,browser)
    if(result.status!=='requested'||result.browser!==browser||result.automation_connected!==false)throw new Error('后台没有返回普通浏览器打开确认，请先检查已打开的窗口。')
    onOpened?.(result,destination.url)
    notify(result.window_reused?'已复用职达原来的 Safari 窗口，没有刷新、重开或改变已填写内容。请在同一窗口继续。':`已请求本机 ${browser==='safari'?'Safari':'Chrome'} 打开招聘网站。请切到浏览器检查实际页面；尚未连接自动填写。`)
    return true
  }catch(error){
    notify(error instanceof Error?error.message:'打开请求未完成，请复制网址到 Safari 或 Chrome 手动打开。')
    return false
  }
}
