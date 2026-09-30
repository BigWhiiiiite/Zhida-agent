import type {ApplicationTarget, ApplicationWorkflowState, BrowserSnapshot, ChatTaskDraft} from './types'

export const CHAT_IMAGE_LIMIT=8*1024*1024
// Mount lazily on first visit, then preserve composer state only for that user.
export function shouldKeepChatMounted(page:string,visitedUserId:string|null,currentUserId:string|null) {
  return Boolean(currentUserId&&(page==='chat'||visitedUserId===currentUserId))
}
export function validateChatImage(file:{type:string;size:number}) {
  if(!['image/png','image/jpeg','image/webp'].includes(file.type)) return '只支持 PNG、JPEG、WEBP 图片，请先转换文件格式。'
  if(file.size>CHAT_IMAGE_LIMIT) return '图片不能超过 8 MB，请压缩后再上传。'
  if(!file.size) return '这张图片是空文件，请重新选择。'
  return ''
}
export function inspectTaskUrl(raw:string) {
  const value=raw.trim()
  if(!value)return {url:'',hostname:'',error:'请补充招聘页面链接；仅有公司名称还不能开始浏览。'}
  if(value.length>2000||/[\u0000-\u0020\u007f\\]/.test(value))return {url:'',hostname:'',error:'链接不得超过 2000 字符，也不能含空白、控制字符或反斜杠。'}
  try {
    const url=new URL(value)
    if(!['http:','https:'].includes(url.protocol)||url.username||url.password)return {url:'',hostname:'',error:'只接受不含账号密码的 HTTP(S) 招聘页面链接。'}
    const host=url.hostname.toLowerCase().replace(/\.$/,'')
    const rawAuthority=value.match(/^https?:\/\/([^/?#]+)/i)?.[1]||''
    if(rawAuthority.includes('%')||!['','80','443'].includes(url.port)||['localhost','metadata','instance-data'].includes(host)||['.localhost','.local','.internal','.intranet','.lan','.home','.home.arpa'].some(ending=>host.endsWith(ending)))return {url:'',hostname:'',error:'请使用公开招聘域名，不能使用内网、本机或特殊端口地址。'}
    // WHATWG parsing canonicalizes integer and hexadecimal hosts to IPv4;
    // reject every literal IP, not only private ranges.
    if(/^\d+\.\d+\.\d+\.\d+$/.test(host)||host.includes(':')||host.startsWith('['))return {url:'',hostname:'',error:'不能使用 IP 地址作为招聘链接，请使用完整公开域名。'}
    const labels=host.split('.')
    if(labels.length<2||labels.some(label=>!(/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/i.test(label)))||!/^(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})$/i.test(labels.at(-1)||''))return {url:'',hostname:'',error:'请输入完整公开域名，不能使用内网或混淆地址。'}
    url.hostname=host
    return {url:url.href,hostname:host,error:''}
  }catch{return {url:'',hostname:'',error:'链接格式不正确，请填写完整网址（包含 https://）。'}}
}
export function targetFromDraft(draft:ChatTaskDraft):ApplicationTarget {
  return {company:draft.company.trim(),job_title:draft.job_title.trim(),city:draft.city.trim(),recruitment_cycle:draft.recruitment_cycle.trim(),source_url:inspectTaskUrl(draft.url).url}
}
export function formStageReady(workflow:ApplicationWorkflowState|null,snapshot:BrowserSnapshot|null) {
  if(!workflow||!snapshot||!['profile_form','application_form','review'].includes(workflow.stage)||workflow.form_fields<=0)return false
  return snapshot.fields.some(field=>!['hidden','button','submit','section-button'].includes(field.field_type))
}
