import type {ApplicationWorkflowState} from './types'

export function observedJobTitle(workflow:ApplicationWorkflowState|null) {
  return workflow?.job_title?.trim()||'尚未从招聘网页识别到岗位名称'
}

export function workflowPageChanged(before:ApplicationWorkflowState|null,after:ApplicationWorkflowState) {
  if(!before)return false
  return before.url!==after.url||before.stage!==after.stage||
    (before.job_title||'').trim()!==(after.job_title||'').trim()||
    before.form_fields!==after.form_fields||before.authenticated!==after.authenticated||
    before.job_id!==after.job_id
}

export function shouldObserveWorkbench(state:{active:boolean;visible:boolean;busy:boolean;sessionId:string;hasWorkflow:boolean}) {
  return state.active&&state.visible&&!state.busy&&Boolean(state.sessionId)&&state.hasWorkflow
}
