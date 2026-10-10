import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import {createRequire} from 'node:module'
import {pathToFileURL} from 'node:url'
import {transformWithOxc} from 'vite'
import {renderToStaticMarkup} from 'react-dom/server'

const require=createRequire(import.meta.url)
const source=await readFile(new URL('./AssistProgress.tsx',import.meta.url),'utf8')
const compiled=(await transformWithOxc(source,'AssistProgress.tsx')).code
  .replace(/from (["'])react\/jsx-runtime\1/g,`from ${JSON.stringify(pathToFileURL(require.resolve('react/jsx-runtime')).href)}`)
const moduleSource=`import React from ${JSON.stringify(pathToFileURL(require.resolve('react')).href)};\n${compiled}`
const {default:AssistProgress}=await import(`data:text/javascript;base64,${Buffer.from(moduleSource).toString('base64')}`)
const failure={label:'合成城市题',message:'网页未读取到可核对的选项'}
const event={kind:'fill',message:'合成回读',completed:1,failed:1,issues:[failure]}
const progress={status:'running',phase:'verify',message:'合成检查',events:[event,{...event,completed:0,failed:0}],cancel_requested:false}
const html=renderToStaticMarkup(AssistProgress({progress,onCancel:()=>{}}))
assert.match(html,/本轮未通过核验的字段/)
assert.match(html,/合成城市题/)
assert.match(html,/网页未读取到可核对的选项/)
assert.equal(html.split('合成城市题').length-1,1,'Repeated failures should not create duplicate user prompts')
assert.match(html,/未提交/)
assert.match(html,/暂停本轮填写/)
assert.doesNotMatch(html,/填写成功|提交成功/)
assert.doesNotMatch(renderToStaticMarkup(AssistProgress({progress:{...progress,events:[]},onCancel:()=>{}})),/本轮未通过核验的字段/)
assert.match(renderToStaticMarkup(AssistProgress({progress:{...progress,status:'interrupted'},onCancel:()=>{}})),/职达已暂停本轮填写/)
const stoppedHtml=renderToStaticMarkup(AssistProgress({progress:{...progress,status:'finished',events:[{
  kind:'write_interrupted',completed:0,failed:0,message:'整批最终核对未完成',issues:[{
    label:'合成姓名题',message:'中断前回读匹配，但整批最终核对未完成，需重新核对'
  }]
}]},onCancel:()=>{}}))
assert.match(stoppedHtml,/仍需重新核对/)
assert.match(stoppedHtml,/本轮回读成功 0 次/)
assert.match(stoppedHtml,/合成姓名题/)
assert.doesNotMatch(stoppedHtml,/填写成功|提交成功|暂停本轮填写/)
const controlHtml=renderToStaticMarkup(AssistProgress({progress:{...progress,phase:'control',
  message:'第3/14项：展开当前题目的面板（本批已用8秒）；尚未通过整批最终核对',
  events:[{kind:'control',completed:0,failed:0,message:'正在定位当前题目'}]},onCancel:()=>{}}))
assert.match(controlHtml,/<li aria-current="step">填写<\/li>/)
assert.match(controlHtml,/展开当前题目的面板/)
assert.match(controlHtml,/本轮回读成功 0 次/)
console.log('assistProgress: visible field failure causes, deduplication and honest status passed')
