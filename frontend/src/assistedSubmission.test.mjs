import {test} from 'node:test'
import assert from 'node:assert/strict'
import {assistedSubmission} from './applicationAssist.ts'

test('D3 starts once and waits for a durable result rather than a long write response',async()=>{
  const calls=[]
  const result=await assistedSubmission(1,{start:async()=>{calls.push('start');return {status:'running'}},
    wait:async()=>{calls.push('receipt');return 'finished'},legacy:async()=>{throw Error('must not use legacy')}})
  assert.equal(result,'finished');assert.deepEqual(calls,['start','receipt'])
})
test('failed receipt never retries start or silently falls back to legacy writes',async()=>{
  let starts=0
  await assert.rejects(assistedSubmission(1,{start:async()=>{starts++},wait:async()=>{throw Error('lost receipt')},
    legacy:async()=>{throw Error('must not run')}}),/lost receipt/)
  assert.equal(starts,1)
})
test('disabled D3 preserves the explicitly advertised legacy contract',async()=>{
  assert.equal(await assistedSubmission(0,{legacy:async()=>42,start:async()=>{throw Error('disabled')},
    wait:async()=>{throw Error('disabled')}}),42)
})
