import assert from 'node:assert/strict'
import {registerHooks} from 'node:module'
registerHooks({resolve(specifier,context,nextResolve){return nextResolve(specifier==='./chatTask'?'./chatTask.ts':specifier,context)}})
const {inspectNavigation,navigationAction}=await import('./workbenchNavigation.ts')
const current='https://360campus.zhiye.com/campus/jobs'
for(const kind of ['open_job','search_jobs','browse_jobs']) {
  const result=inspectNavigation({kind,url:''},current)
  assert.equal(result.error,'')
  assert.equal(result.samePage,true)
  assert.equal(result.url,current)
}
assert.equal(inspectNavigation({kind:'open_job',url:'https://example.test/jobs/123'},current).samePage,false)
for(const url of ['javascript:alert(1)','http://localhost/','http://127.0.0.1/','https://example.test:8000/']) {
  assert.ok(inspectNavigation({kind:'open_job',url},current).error)
  assert.ok(inspectNavigation({kind:'search_jobs',url:''},url).error)
}
assert.ok(inspectNavigation({kind:'submit',url:''},current).error)
assert.ok(inspectNavigation({kind:'search_jobs',url:''},'').error)
assert.equal(navigationAction('search_jobs'),'搜索目标岗位')
console.log('workbench navigation: same-page controls and unsafe destinations passed')
