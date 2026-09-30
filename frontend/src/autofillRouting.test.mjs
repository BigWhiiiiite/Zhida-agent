import assert from 'node:assert/strict'
import {actionReviewTitle,mergePhaseExecutions,modelPending,routeLabel,runAutofillPhases} from './autofillRouting.ts'

const result=(selector,status='filled',verified=true)=>({selector,label:'示例字段',status,verified,actual_value:'示例值',message:''})
const execution=(results,tag)=>({url:'https://careers.example.test/form',results,completed:results.filter(row=>row.status==='filled').length,verified:results.filter(row=>row.verified).length,failed:results.filter(row=>row.status==='failed').length,skipped:0,unverified:0,pre_submit:{tag}})
const phaseResult=(phase,pending,results)=>({phase,review:{plan:{actions:[],routing_summary:{rules_ready:1,model_resolved:phase==='model'?1:0,needs_user:0,model_pending:pending}}},execution:execution(results,phase)})

assert.equal(actionReviewTitle({review_question:'是否接受其他城市安排？',label:'是'},{label:'是'}),'是否接受其他城市安排？')
assert.equal(actionReviewTitle({}, {question_text:'期望工作城市'}),'期望工作城市')
assert.equal(routeLabel({needs_model:true,resolution_source:'user'}),'待模型分析')
assert.equal(routeLabel({user_confirmed:true,resolution_source:'model'}),'用户已确认')
assert.equal(modelPending({actions:[{needs_model:true},{needs_model:false}]}),1)
assert.equal(modelPending({actions:[{needs_model:true}],routing_summary:{model_pending:0}}),0)
const merged=mergePhaseExecutions(execution([result('#name'),result('#city','failed',false)],'rules'),execution([result('#city'),result('#college','failed',false)],'model'))
assert.equal(merged.results.length,3)
assert.equal(merged.verified,2)
assert.equal(merged.failed,1)
assert.equal(merged.pre_submit.tag,'model')

const calls=[]
const complete=await runAutofillPhases(async phase=>{calls.push('request:'+phase);return phaseResult(phase,phase==='rules'?1:0,[result(phase==='rules'?'#name':'#city')])},phase=>calls.push('display:'+phase.phase),()=>calls.push('start-model'))
assert.deepEqual(calls,['request:rules','display:rules','start-model','request:model','display:model'])
assert.equal(complete.execution.verified,2)
const noModel=[]
await runAutofillPhases(async phase=>{noModel.push(phase);return phaseResult(phase,0,[result('#name')])},()=>{},()=>assert.fail('Model must not run without pending fields'))
assert.deepEqual(noModel,['rules'])
let retained=null
await assert.rejects(runAutofillPhases(async phase=>{if(phase==='model')throw new Error('Synthetic model unavailable');return phaseResult(phase,1,[result('#name')])},(phase,current)=>{retained=current},()=>{}),/Synthetic model unavailable/)
assert.equal(retained.verified,1)
console.log('autofill routing: 15 synthetic assertions passed')
