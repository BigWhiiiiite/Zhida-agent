import assert from 'node:assert/strict'
import {createRequire} from 'node:module'
import {fileURLToPath} from 'node:url'
import vm from 'node:vm'
import {build} from 'vite'

const built=await build({configFile:false,logLevel:'error',build:{write:false,minify:false,
  lib:{entry:fileURLToPath(new URL('./normalWebsiteNavigation.ts',import.meta.url)),formats:['cjs'],fileName:'navigation-test'},
}})
const result=Array.isArray(built)?built[0]:built
const module={exports:{}}
vm.runInNewContext(result.output.find(file=>file.type==='chunk').code,{module,exports:module.exports,require:createRequire(import.meta.url),URL,Error})
const {requestExternalWebsite}=module.exports

for(const browser of ['safari','chrome']){
  const calls=[],notices=[]
  const url='https://eoap.cebbank.com/uiap/wt/CEB/zpzh/campus?job=ai#apply'
  const ok=await requestExternalWebsite(url,browser,async(...args)=>{
    calls.push(args);return {status:'requested',browser,automation_connected:false}
  },message=>notices.push(message))
  assert.equal(ok,true)
  assert.deepEqual(calls,[[url,browser]])
  assert.match(notices[0],/已请求本机/)
  assert.match(notices[0],/尚未连接自动填写/)
  assert.doesNotMatch(notices[0],/已成功|打开成功/)
}
for(const url of ['', 'javascript:alert(1)', 'https://user:password@jobs.example.test/', 'http://127.0.0.1:8000']){
  let called=false
  assert.equal(await requestExternalWebsite(url,'safari',async()=>{called=true;throw Error('unexpected')},()=>{}),false)
  assert.equal(called,false)
}
for(const result of [{status:'loaded',browser:'safari',automation_connected:false},{status:'requested',browser:'chrome',automation_connected:false},{status:'requested',browser:'safari',automation_connected:true}]){
  assert.equal(await requestExternalWebsite('https://jobs.example.test/','safari',async()=>result,()=>{}),false)
}
let calls=0
const notices=[]
assert.equal(await requestExternalWebsite('https://jobs.example.test/','safari',async()=>{calls++;throw Error('后端未连接')},message=>notices.push(message)),false)
assert.equal(calls,1,'Never silently retry or fall back to another browser')
assert.equal(notices[0],'后端未连接')
const remembered=[],reusedNotices=[]
const reused={status:'requested',browser:'safari',automation_connected:false,safari_window_token:'opaque-user-owned-token',window_reused:true}
assert.equal(await requestExternalWebsite('https://jobs.example.test/campus','safari',async()=>reused,message=>reusedNotices.push(message),(result,url)=>remembered.push({result,url})),true)
assert.deepEqual(remembered,[{result:reused,url:'https://jobs.example.test/campus'}])
assert.match(reusedNotices[0],/没有刷新、重开/)
assert.doesNotMatch(reusedNotices[0],/填写成功|已提交/)
console.log('externalWebsiteOpening: OK (explicit browser, validation, request-only status, no silent retry/fallback)')
