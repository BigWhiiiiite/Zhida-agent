import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { registerHooks } from 'node:module'
import { fileURLToPath } from 'node:url'
import { transformWithOxc } from 'vite'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

const componentUrl = new URL('./ApplicationWorkspace.tsx', import.meta.url)
const compiled = await transformWithOxc(readFileSync(componentUrl, 'utf8'), fileURLToPath(componentUrl), { jsx: { runtime: 'automatic' } })

registerHooks({
  load(url, context, nextLoad) {
    if (url.endsWith('/application-workspace.css')) return { format: 'module', source: '', shortCircuit: true }
    if (url.endsWith('/ApplicationWorkspace.tsx')) {
      return { format: 'module', source: compiled.code, shortCircuit: true }
    }
    return nextLoad(url, context)
  },
})

const { default: ApplicationWorkspace, validateApplicationSetup } = await import('./ApplicationWorkspace.tsx')
const resumes = [{ id: 'anonymous-resume', label: '匿名测试简历' }, { id: 'unavailable', label: '尚未解析', disabled: true }]
assert.equal(validateApplicationSetup(resumes, '', '').valid, false)
assert.equal(validateApplicationSetup([], 'deleted-resume', 'https://example.com/apply').valid, false)
assert.equal(validateApplicationSetup(resumes, 'unavailable', 'https://example.com/apply').valid, false)
for (const url of ['example.com/apply', 'javascript:alert(1)', 'file:///tmp/test', 'https://user:password@example.com/apply']) {
  assert.equal(validateApplicationSetup(resumes, 'anonymous-resume', url).valid, false, url)
}
assert.deepEqual(validateApplicationSetup(resumes, 'anonymous-resume', '  https://example.com/apply?job=123  ').selection,
  { resumeId: 'anonymous-resume', url: 'https://example.com/apply?job=123' })
assert.equal(validateApplicationSetup(resumes, 'anonymous-resume', 'https://example.com/apply?job=123').valid, true)

const base = {
  stage: 'setup', section: 'operation', resumes, resumeId: 'anonymous-resume', url: 'https://example.com/apply',
  onSectionChange() {}, onResumeChange() {}, onUrlChange() {}, onContinue() {}, onBackToSetup() {},
  pendingCount: 2,
  operation: createElement('p', {}, '匿名操作槽'),
  questions: createElement('p', {}, '匿名补充槽'),
  diagnostics: createElement('p', {}, '匿名诊断槽'),
}
const render = props => renderToStaticMarkup(createElement(ApplicationWorkspace, { ...base, ...props }))
let html = render({})
assert.match(html, /网址必须是信息填写页，登录和选岗已由用户完成/)
assert.match(html, /确认并继续/)
assert.doesNotMatch(html, /匿名操作槽|匿名补充槽|匿名诊断槽|开发检查/)
assert.doesNotMatch(html, /type="submit" disabled/)
html = render({ resumeId: '', url: '' })
assert.match(html, /type="submit" disabled/)
html = render({ resumes: [], resumeId: '' })
assert.match(html, /请先在简历资料库添加一份可用的简历/)
html = render({ stage: 'workspace', section: 'operation' })
assert.match(html, /匿名操作槽/)
assert.doesNotMatch(html, /确认并继续/)
assert.match(html, /data-workspace-section="operation"[^>]*>[^<]*<p>匿名操作槽/)
assert.match(html, /data-workspace-section="questions"[^>]*hidden=""/)
assert.match(html, /data-workspace-section="diagnostics"[^>]*hidden=""/)
assert.match(html, /aria-current="page"/)
assert.match(html, /招聘官网页面会保留/)
assert.match(html, /aria-label="2 项待补充"/)
html = render({ stage: 'workspace', section: 'questions' })
assert.match(html, /匿名补充槽/)
assert.match(html, /data-workspace-section="operation"[^>]*hidden=""/)
assert.match(html, /data-workspace-section="diagnostics"[^>]*hidden=""/)
assert.doesNotMatch(html, /data-workspace-section="questions"[^>]*hidden=""/)
html = render({ stage: 'workspace', section: 'diagnostics' })
assert.match(html, /匿名诊断槽/)
assert.match(html, /data-workspace-section="operation"[^>]*hidden=""/)
assert.match(html, /data-workspace-section="questions"[^>]*hidden=""/)
assert.doesNotMatch(html, /data-workspace-section="diagnostics"[^>]*hidden=""/)
html = render({ stage: 'workspace', section: 'questions', questions: undefined, pendingCount: -1 })
assert.match(html, /目前没有需要补充的资料/)
assert.doesNotMatch(html, /项待补充/)
console.log('application workspace: anonymous setup validation and isolated section rendering passed')
