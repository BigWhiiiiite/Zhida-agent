// Offline component contract: no GUI, live website, or backend requests.
import assert from 'node:assert/strict'
import {createRequire} from 'node:module'
import {fileURLToPath} from 'node:url'
import vm from 'node:vm'
import {build} from 'vite'
import {createElement} from 'react'
import {renderToStaticMarkup} from 'react-dom/server'

const buildResult=await build({configFile:false,logLevel:'error',build:{
  write:false,minify:false,
  lib:{entry:fileURLToPath(new URL('./RecruitingWebsiteLink.tsx',import.meta.url)),formats:['cjs'],fileName:'opening-test'},
  rolldownOptions:{external:['react','react/jsx-runtime','lucide-react']},
}})
const result=Array.isArray(buildResult)?buildResult[0]:buildResult
const module={exports:{}}
vm.runInNewContext(result.output.find(file=>file.type==='chunk').code,{
  module,exports:module.exports,require:createRequire(import.meta.url),URL,
})
const {RecruitingWebsiteLink}=module.exports
for(const url of [
  'https://eoap.cebbank.com/uiap/wt/CEB/zpzhm/campus',
  'https://eoap.cebbank.com/uiap/wt/CEB/zpzh/campus',
  'https://jobs.example.test/campus?jobAdId=agent#apply',
]){
  const notices=[]
  const element=RecruitingWebsiteLink({url,notify:text=>notices.push(text)})
  assert.equal(element.type,'a')
  assert.equal(element.props.href,url,'Do not rewrite distinct recruiting routes, query, or hash')
  assert.equal(element.props.target,'_blank')
  assert.equal(element.props.rel,'noopener noreferrer')
  let prevented=false
  element.props.onClick({preventDefault(){prevented=true}})
  assert.equal(prevented,false,'Allow ordinary browser navigation; no automation confirmation')
  assert.match(notices[0],/尚未连接自动填写/)
  assert.doesNotMatch(notices[0],/打开成功|已成功/,'A click cannot prove SPA rendering')
  const html=renderToStaticMarkup(createElement(RecruitingWebsiteLink,{url,notify(){}}))
  assert.match(html,/<a /)
  assert.match(html,/target="_blank"/)
}
for(const url of ['', 'javascript:alert(1)', 'https://user:password@jobs.example.test/', 'http://127.0.0.1:8000']){
  const notices=[]
  const element=RecruitingWebsiteLink({url,notify:text=>notices.push(text)})
  assert.equal(element.props.href,undefined)
  assert.equal(element.props['aria-disabled'],true)
  let prevented=false
  element.props.onClick({preventDefault(){prevented=true}})
  assert.equal(prevented,true)
  assert.ok(notices[0])
}
const controlsBuild=await build({configFile:false,logLevel:'error',build:{
  write:false,minify:false,
  lib:{entry:fileURLToPath(new URL('./ExternalWebsiteOpening.tsx',import.meta.url)),formats:['cjs'],fileName:'controls-test'},
  rolldownOptions:{external:['react','react/jsx-runtime','lucide-react']},
}})
const controlsResult=Array.isArray(controlsBuild)?controlsBuild[0]:controlsBuild
const controlsModule={exports:{}}
vm.runInNewContext(controlsResult.output.find(file=>file.type==='chunk').code,{
  module:controlsModule,exports:controlsModule.exports,require:createRequire(import.meta.url),URL,
})
const html=renderToStaticMarkup(createElement(controlsModule.exports.ExternalWebsiteOpening,{
  url:'https://jobs.example.test/campus',notify(){},openExternal(){throw Error('SSR must not launch browser')},
}))
assert.match(html,/Safari（推荐）/)
assert.match(html,/value="safari" selected=""/)
assert.match(html,/Google Chrome/)
assert.match(html,/用 Safari 打开招聘网站/)
assert.match(html,/复制网址/)
assert.match(html,/VS Code/)
console.log('RecruitingWebsiteLink: OK (normal link, explicit native-browser controls, truthful status, safe URLs)')
