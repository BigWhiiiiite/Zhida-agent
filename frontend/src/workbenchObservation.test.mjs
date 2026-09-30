import assert from 'node:assert/strict'
import {observedJobTitle,shouldObserveWorkbench,workflowPageChanged} from './workbenchObservation.ts'
const before={url:'https://jobs.example.test/form#basic',stage:'unknown',job_title:'',form_fields:0,authenticated:false,job_id:'example-role',target:{job_title:'希望申请的职位'}}
assert.equal(observedJobTitle(before),'尚未从招聘网页识别到岗位名称')
assert.equal(observedJobTitle({...before,job_title:' 网页中的岗位名称 '}),'网页中的岗位名称')
assert.equal(observedJobTitle(null),'尚未从招聘网页识别到岗位名称')
assert.equal(workflowPageChanged(before,{...before}),false)
assert.equal(workflowPageChanged(before,{...before,target:{job_title:'修改后的目标'}}),false,'User target is not observed page evidence')
for(const patch of [{url:'https://jobs.example.test/form#education'},{stage:'application_form'},{job_title:'网页显示的岗位'},{form_fields:29},{authenticated:true},{job_id:'different-role'}])assert.equal(workflowPageChanged(before,{...before,...patch}),true)
assert.equal(workflowPageChanged(null,before),false)
const ready={active:true,visible:true,busy:false,sessionId:'example-session',hasWorkflow:true}
assert.equal(shouldObserveWorkbench(ready),true)
for(const patch of [{active:false},{visible:false},{busy:true},{sessionId:''},{hasWorkflow:false}])assert.equal(shouldObserveWorkbench({...ready,...patch}),false)
console.log('workbench observation: 18 synthetic assertions passed')
