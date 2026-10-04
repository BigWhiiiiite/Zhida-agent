import {inspectTaskUrl} from './chatTask'
import type {NavigationCandidate} from './types'

// An observed, current candidate may be an in-page control rather than a link.
// The server re-observes its ID immediately before acting; never invent a URL.
export function inspectNavigation(candidate:NavigationCandidate,currentUrl:string) {
  const samePage=!candidate.url.trim()
  if(!['open_job','search_jobs','browse_jobs'].includes(candidate.kind)) {
    return {url:'',hostname:'',samePage,error:'这不是可用的招聘导航入口，请重新识别。'}
  }
  return {...inspectTaskUrl(samePage?currentUrl:candidate.url),samePage}
}

export function navigationAction(kind:NavigationCandidate['kind']) {
  return kind==='search_jobs'?'搜索目标岗位':kind==='open_job'?'打开这个岗位':'进入招聘入口'
}
