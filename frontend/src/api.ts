import type { ApplicationWorkflowState, BrowserSnapshot, CandidateProfile, Conflict, ExecutionResult, FillAction, FormPlan, PreSubmitCheck, ResumeProfile, ResumeRecord } from './types'

export const API = import.meta.env.VITE_API_URL ?? 'http://localhost:8000/api'

export class ApiError extends Error{
  status:number; detail:unknown
  constructor(message:string,status:number,detail:unknown){super(message);this.name='ApiError';this.status=status;this.detail=detail}
}

async function result<T>(response:Response):Promise<T>{
  if(!response.ok){const body=await response.json().catch(()=>({})); const detail=body.detail; throw new ApiError(typeof detail==='string'?detail:(detail?.message??'请求失败'),response.status,detail)}
  if(response.status===204) return undefined as T
  return response.json()
}

export const api={
  profile:()=>fetch(`${API}/profile`).then(result<CandidateProfile>),
  saveProfile:(profile:ResumeProfile)=>fetch(`${API}/profile`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(profile)}).then(result<CandidateProfile>),
  list:()=>fetch(`${API}/resumes`).then(result<ResumeRecord[]>),
  upload:(file:File)=>{const data=new FormData();data.append('file',file);return fetch(`${API}/resumes`,{method:'POST',body:data}).then(result<ResumeRecord>)},
  saveResume:(id:string,patch:Partial<ResumeRecord>)=>fetch(`${API}/resumes/${id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)}).then(result<ResumeRecord>),
  deleteResume:(id:string)=>fetch(`${API}/resumes/${id}`,{method:'DELETE'}).then(result<void>),
  reparse:(id:string)=>fetch(`${API}/resumes/${id}/parse`,{method:'POST'}).then(result<ResumeRecord>),
  text:(id:string)=>fetch(`${API}/resumes/${id}/text`).then(result<{text:string}>),
  review:(resumeId:string,evidenceId:string,status:string,value?:unknown)=>fetch(`${API}/resumes/${resumeId}/evidence/${evidenceId}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status,value})}).then(result<ResumeRecord>),
  conflicts:()=>fetch(`${API}/conflicts`).then(result<Conflict[]>),
  resolve:(id:string,choice:'current'|'incoming'|'custom',custom_value?:unknown)=>fetch(`${API}/conflicts/${id}/resolve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({choice,custom_value})}).then(result<Conflict>),
  startBrowser:(url:string)=>fetch(`${API}/browser/start`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})}).then(result<BrowserSnapshot>),
  browserSnapshot:(id:string)=>fetch(`${API}/browser/${id}/snapshot`).then(result<BrowserSnapshot>),
  workflowState:(id:string)=>fetch(`${API}/browser/${id}/workflow`).then(result<ApplicationWorkflowState>),
  advanceWorkflow:(id:string,intent:'start_application'|'refresh')=>fetch(`${API}/browser/${id}/workflow/advance`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({intent})}).then(result<ApplicationWorkflowState>),
  requestVerificationCode:(id:string,channel:'phone'|'email')=>fetch(`${API}/browser/${id}/workflow/request-code`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel})}).then(result<ApplicationWorkflowState>),
  enterVerificationCode:(id:string,code:string,submit:boolean)=>fetch(`${API}/browser/${id}/workflow/verification`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,submit})}).then(result<ApplicationWorkflowState>),
  planForm:(id:string)=>fetch(`${API}/browser/${id}/plan`,{method:'POST'}).then(result<FormPlan>),
  executeForm:(id:string,actions:FillAction[],resume_id:string)=>fetch(`${API}/browser/${id}/execute`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actions,min_confidence:.85,resume_id})}).then(result<ExecutionResult>),
  checkForm:(id:string)=>fetch(`${API}/browser/${id}/check`).then(result<PreSubmitCheck>),
  closeBrowser:(id:string)=>fetch(`${API}/browser/${id}`,{method:'DELETE'}).then(result<void>),
}
