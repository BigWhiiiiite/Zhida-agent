import type { ApplicationQueueItem, ApplicationReadiness, ApplicationWorkflowState, AuthSession, BrowserSnapshot, CandidateProfile, Conflict, ExecutionResult, FillAction, FormPlan, FormReviewResult, JobDiscoveryResult, JobVerification, ModelHealth, NativeResumeImportResult, OfficialJobSource, PreSubmitCheck, QueueStatus, RecommendationBatch, ResumeProfile, ResumeRecord, UserAccount } from './types'

export const API = import.meta.env.VITE_API_URL ?? `${window.location.protocol}//${window.location.hostname}:8000/api`
const nativeFetch=window.fetch.bind(window)
const fetch=(input:RequestInfo|URL,init:RequestInit={})=>nativeFetch(input,{...init,credentials:'include'})

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
  me:()=>fetch(`${API}/auth/me`).then(result<UserAccount>),
  register:(email:string,password:string,display_name:string)=>fetch(`${API}/auth/register`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password,display_name})}).then(result<AuthSession>),
  login:(email:string,password:string)=>fetch(`${API}/auth/login`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})}).then(result<AuthSession>),
  logout:()=>fetch(`${API}/auth/logout`,{method:'POST'}).then(result<void>),
  profile:()=>fetch(`${API}/profile`).then(result<CandidateProfile>),
  saveProfile:(profile:ResumeProfile)=>fetch(`${API}/profile`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(profile)}).then(result<CandidateProfile>),
  saveApplicationAnswer:(question:string,field_name:string,value:string)=>fetch(`${API}/profile/application-answer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question,field_name,value})}).then(result<CandidateProfile>),
  recommendations:(location='')=>fetch(`${API}/jobs/recommendations${location?`?location=${encodeURIComponent(location)}`:''}`).then(result<RecommendationBatch>),
  jobSources:()=>fetch(`${API}/jobs/sources`).then(result<OfficialJobSource[]>),
  syncJobSource:(id:string)=>fetch(`${API}/jobs/sources/${encodeURIComponent(id)}/sync`,{method:'POST'}).then(result<JobDiscoveryResult>),
  verifyJob:(id:string)=>fetch(`${API}/jobs/${encodeURIComponent(id)}/verify`,{method:'POST'}).then(result<JobVerification>),
  readiness:()=>fetch(`${API}/readiness`).then(result<ApplicationReadiness>),
  modelHealth:()=>fetch(`${API}/model/health`,{method:'POST'}).then(result<ModelHealth>),
  jobQueue:()=>fetch(`${API}/jobs/queue`).then(result<ApplicationQueueItem[]>),
  queueJobs:(job_ids:string[],resume_id:string)=>fetch(`${API}/jobs/queue`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_ids,resume_id})}).then(result<ApplicationQueueItem[]>),
  updateQueuedJob:(id:string,patch:{status?:QueueStatus;notes?:string;application_id?:string;candidate_confirmed?:boolean})=>fetch(`${API}/jobs/queue/${id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)}).then(result<ApplicationQueueItem>),
  removeQueuedJob:(id:string)=>fetch(`${API}/jobs/queue/${id}`,{method:'DELETE'}).then(result<void>),
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
  expandBrowserSection:(id:string,selector:string)=>fetch(`${API}/browser/${id}/expand`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selector})}).then(result<BrowserSnapshot>),
  importResumeWithSite:(id:string,resume_id:string)=>fetch(`${API}/browser/${id}/native-resume`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resume_id})}).then(result<NativeResumeImportResult>),
  workflowState:(id:string)=>fetch(`${API}/browser/${id}/workflow`).then(result<ApplicationWorkflowState>),
  advanceWorkflow:(id:string,intent:'start_application'|'create_account'|'continue_application'|'refresh')=>fetch(`${API}/browser/${id}/workflow/advance`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({intent})}).then(result<ApplicationWorkflowState>),
  fillRegistration:(id:string,email:string,phone:string,password:string)=>fetch(`${API}/browser/${id}/workflow/registration`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,phone,password})}).then(result<ApplicationWorkflowState>),
  requestVerificationCode:(id:string,channel:'phone'|'email',value='')=>fetch(`${API}/browser/${id}/workflow/request-code`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel,value})}).then(result<ApplicationWorkflowState>),
  enterVerificationCode:(id:string,code:string,submit:boolean)=>fetch(`${API}/browser/${id}/workflow/verification`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,submit})}).then(result<ApplicationWorkflowState>),
  planForm:(id:string)=>fetch(`${API}/browser/${id}/plan`,{method:'POST'}).then(result<FormPlan>),
  reviewForm:(id:string,useModel=false)=>fetch(`${API}/browser/${id}/review${useModel?'?use_model=true':''}`,{method:'POST'}).then(result<FormReviewResult>),
  executeForm:(id:string,actions:FillAction[],resume_id:string)=>fetch(`${API}/browser/${id}/execute`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actions,min_confidence:.85,resume_id})}).then(result<ExecutionResult>),
  checkForm:(id:string)=>fetch(`${API}/browser/${id}/check`).then(result<PreSubmitCheck>),
  closeBrowser:(id:string)=>fetch(`${API}/browser/${id}`,{method:'DELETE'}).then(result<void>),
}
