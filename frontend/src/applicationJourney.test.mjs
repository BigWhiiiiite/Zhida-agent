import assert from 'node:assert/strict'
import {journeyBackendReady,journeyButtonLabel,journeyGuard,journeyStatus} from './applicationJourney.ts'
import {learnVerifiedDrafts} from './applicationAssist.ts'

assert.equal(journeyBackendReady({}),false)
assert.equal(journeyBackendReady({journey_version:0}),false)
assert.equal(journeyBackendReady({journey_version:'1'}),false)
assert.equal(journeyBackendReady({journey_version:1}),true)
for(const [stage,text] of [['auth_required','登录'],['registration_required','注册'],['verification_required','验证']])assert.match(journeyButtonLabel(stage),new RegExp(`我已完成${text}`))
assert.equal(journeyButtonLabel('application_form'),'让职达继续')
for(const status of ['waiting_login','waiting_registration','waiting_verification','needs_user','ready_for_review','blocked','partial'])assert.doesNotMatch(journeyStatus(status),/已投递|提交成功|投递成功/)
assert.match(journeyStatus('ready_for_review'),/尚未提交/)
const state={busy:false,available:true,resumeId:'cv-a',hasDraftEdits:false}
assert.equal(journeyGuard(state),'')
for(const [key,value] of Object.entries({busy:true,available:false,resumeId:'',hasDraftEdits:true}))assert.notEqual(journeyGuard({...state,[key]:value}),'')

const calls=[]
const result=await learnVerifiedDrafts({saved:'a',memoryFailed:'b',fillFailed:'c',unverified:'d',sensitive:'e'}, {results:[
  {selector:'saved',status:'filled',verified:true},
  {selector:'memoryFailed',status:'filled',verified:true},
  {selector:'fillFailed',status:'failed',verified:false},
  {selector:'unverified',status:'filled',verified:false},
  {selector:'sensitive',status:'filled',verified:true},
]},async(selector)=>{calls.push(selector);if(selector==='memoryFailed')throw new Error('offline');return selector!=='sensitive'})
assert.deepEqual(result,{count:1,failed:['memoryFailed']},'A memory failure is surfaced separately from successful web filling')
assert.deepEqual(calls,['saved','memoryFailed','sensitive'],'Failed/unverified web writes are never learned')
console.log('application journey: capability gate, truthful human stages, user boundaries, visible memory failures passed')
