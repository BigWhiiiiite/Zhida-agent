import type { ApplicationTarget, ChatConversation, ChatConversationSummary, ApplicationAgentTurn, ApplicationKnowledgeInput, ApplicationKnowledgeRecord, KnowledgeMappingTarget, ApplicationQueueItem, ApplicationReadiness, ApplicationWorkflowState, AuthSession, AutofillPhaseResult, BrowserSnapshot, CandidateProfile, CompanySize, Conflict, DiscoveryAdapter, ExecutionResult, FillAction, FormPlan, FormReviewResult, JobDiscoveryResult, JobEvidenceExplanation, JobVerification, ModelHealth, NativeResumeImportResult, OfficialJobSource, PreSubmitCheck, QueueStatus, RecommendationBatch, ResumeProfile, ResumeRecord, SmartJobSearchResult, UserAccount } from './types'
import type {ApplicationAssistRequest, ApplicationAssistResult, ApplicationAssistProgress, ApplicationJourneyResult, ConfirmedResumeFact, ResumeFactTargets} from './types'
import {resumeAttachmentRequest} from './resumeAttachment'
import type {ObservationConsent,PageRegionObservation,ExtractionAuditRequest,ExtractionAuditResult} from './types'

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
  previewExistingSafari:(url:string)=>fetch(`${API}/websites/safari/preview-existing`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url})}).then(result<{preview_token:string;url:string;expires_in_seconds:number}>),
  confirmExistingSafari:(url:string,previewToken:string)=>fetch(`${API}/websites/safari/confirm-existing`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,preview_token:previewToken})}).then(result<{status:'requested';browser:'safari';automation_connected:false;safari_window_token:string;window_reused:true}>),
  openExternalWebsite:(url:string,browser:'safari'|'chrome')=>fetch(`${API}/websites/open`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,browser})}).then(result<{status:'requested';browser:'safari'|'chrome';automation_connected:false;safari_window_token?:string;window_reused?:boolean}>),
  me:()=>fetch(`${API}/auth/me`).then(result<UserAccount>),
  register:(email:string,password:string,display_name:string)=>fetch(`${API}/auth/register`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password,display_name})}).then(result<AuthSession>),
  login:(email:string,password:string)=>fetch(`${API}/auth/login`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,password})}).then(result<AuthSession>),
  logout:()=>fetch(`${API}/auth/logout`,{method:'POST'}).then(result<void>),
  profile:()=>fetch(`${API}/profile`).then(result<CandidateProfile>),
  applicationKnowledge:()=>fetch(`${API}/application-knowledge`).then(result<ApplicationKnowledgeRecord[]>),
  applicationKnowledgeTargets:()=>fetch(`${API}/application-knowledge/targets`).then(result<KnowledgeMappingTarget[]>),
  saveApplicationKnowledge:(payload:ApplicationKnowledgeInput)=>fetch(`${API}/application-knowledge`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(result<ApplicationKnowledgeRecord>),
  deleteApplicationKnowledge:(id:string)=>fetch(`${API}/application-knowledge/${encodeURIComponent(id)}`,{method:'DELETE'}).then(result<void>),
  saveProfile:(profile:ResumeProfile)=>fetch(`${API}/profile`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(profile)}).then(result<CandidateProfile>),
  saveApplicationAnswer:(question:string,field_name:string,value:string,metadata:Partial<{semantic_key:string;entity_scope:string;field_signature:string;field_type:string;options:string[];source_url:string;resume_id:string}>={})=>fetch(`${API}/profile/application-answer`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question,field_name,value,...metadata})}).then(result<CandidateProfile>),
  recommendations:(location='')=>fetch(`${API}/jobs/recommendations${location?`?location=${encodeURIComponent(location)}`:''}`).then(result<RecommendationBatch>),
  ragRecommendations:(payload:{location:string;query:string;company_sizes:CompanySize[]})=>fetch(`${API}/jobs/recommendations/rag`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(result<RecommendationBatch>),
  explainJob:(id:string)=>fetch(`${API}/jobs/${encodeURIComponent(id)}/explain`,{method:'POST'}).then(result<JobEvidenceExplanation>),
  jobSources:()=>fetch(`${API}/jobs/sources`).then(result<OfficialJobSource[]>),
  addJobSource:(payload:{company:string;official_url:string;adapter:DiscoveryAdapter;source_key?:string;company_size:CompanySize})=>fetch(`${API}/jobs/sources`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(result<OfficialJobSource>),
  deleteJobSource:(id:string)=>fetch(`${API}/jobs/sources/${encodeURIComponent(id)}`,{method:'DELETE'}).then(result<void>),
  syncJobSource:(id:string)=>fetch(`${API}/jobs/sources/${encodeURIComponent(id)}/sync`,{method:'POST'}).then(result<JobDiscoveryResult>),
  smartJobSearch:(payload:{query:string;location:string;company_sizes:CompanySize[];sync_sources:boolean;max_sources?:number})=>fetch(`${API}/jobs/search`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(result<SmartJobSearchResult>),
  verifyJob:(id:string)=>fetch(`${API}/jobs/${encodeURIComponent(id)}/verify`,{method:'POST'}).then(result<JobVerification>),
  readiness:()=>fetch(`${API}/readiness`).then(result<ApplicationReadiness>),
  modelHealth:()=>fetch(`${API}/model/health`,{method:'POST'}).then(result<ModelHealth>),
  jobQueue:()=>fetch(`${API}/jobs/queue`).then(result<ApplicationQueueItem[]>),
  queueJobs:(job_ids:string[],resume_id:string)=>fetch(`${API}/jobs/queue`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({job_ids,resume_id})}).then(result<ApplicationQueueItem[]>),
  updateQueuedJob:(id:string,patch:{status?:QueueStatus;notes?:string;application_id?:string;candidate_confirmed?:boolean})=>fetch(`${API}/jobs/queue/${id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)}).then(result<ApplicationQueueItem>),
  removeQueuedJob:(id:string)=>fetch(`${API}/jobs/queue/${id}`,{method:'DELETE'}).then(result<void>),
  list:()=>fetch(`${API}/resumes`).then(result<ResumeRecord[]>),
  resumeFactTargets:(id:string)=>fetch(`${API}/resumes/${encodeURIComponent(id)}/fact-targets`).then(result<ResumeFactTargets>),
  confirmResumeFact:(id:string,payload:ConfirmedResumeFact)=>fetch(`${API}/resumes/${encodeURIComponent(id)}/confirmed-fact`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(result<ResumeRecord>),
  upload:(file:File)=>{const data=new FormData();data.append('file',file);return fetch(`${API}/resumes`,{method:'POST',body:data}).then(result<ResumeRecord>)},
  saveResume:(id:string,patch:Partial<ResumeRecord>)=>fetch(`${API}/resumes/${id}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(patch)}).then(result<ResumeRecord>),
  deleteResume:(id:string)=>fetch(`${API}/resumes/${id}`,{method:'DELETE'}).then(result<void>),
  reparse:(id:string)=>fetch(`${API}/resumes/${id}/parse`,{method:'POST'}).then(result<ResumeRecord>),
  text:(id:string)=>fetch(`${API}/resumes/${id}/text`).then(result<{text:string}>),
  review:(resumeId:string,evidenceId:string,status:string,value?:unknown)=>fetch(`${API}/resumes/${resumeId}/evidence/${evidenceId}`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({status,value})}).then(result<ResumeRecord>),
  conflicts:()=>fetch(`${API}/conflicts`).then(result<Conflict[]>),
  resolve:(id:string,choice:'current'|'incoming'|'custom',custom_value?:unknown)=>fetch(`${API}/conflicts/${id}/resolve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({choice,custom_value})}).then(result<Conflict>),
  chatConversations:()=>fetch(`${API}/chat/conversations`).then(result<ChatConversationSummary[]>),
  createChat:()=>fetch(`${API}/chat/conversations`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(result<ChatConversation>),
  chatConversation:(id:string)=>fetch(`${API}/chat/conversations/${encodeURIComponent(id)}`).then(result<ChatConversation>),
  saveChatDraft:(id:string,draft:{company:string;job_title:string;city:string;recruitment_cycle:string;url:string;intent:'apply'|'recommend'|'profile'|'clarify'})=>fetch(`${API}/chat/conversations/${encodeURIComponent(id)}/draft`,{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify(draft)}).then(result<ChatConversation>),
  sendChatMessage:(id:string,text:string,image:File|null,consent:boolean)=>{const data=new FormData();data.append('text',text);data.append('model_consent',String(consent));if(image)data.append('image',image);return fetch(`${API}/chat/conversations/${encodeURIComponent(id)}/messages`,{method:'POST',body:data}).then(result<ChatConversation>)},
  startBrowser:(url:string,target?:ApplicationTarget,resume_id='',safari_window_token='')=>fetch(`${API}/browser/start`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({url,target,resume_id,safari_window_token})}).then(result<BrowserSnapshot>),
  currentBrowser:()=>fetch(`${API}/browser/current`).then(result<{session_id:string|null;occupied:boolean;resume_id:string;assistance_version?:number;journey_version?:number;record_completion_version?:number}>),
  browserSnapshot:(id:string)=>fetch(`${API}/browser/${id}/snapshot`).then(result<BrowserSnapshot>),
  recognitionDiagnostics:(id:string)=>fetch(`${API}/browser/${id}/recognition-diagnostics`).then(result<Record<string,unknown>>),
  observationConsent:(id:string)=>fetch(`${API}/browser/${id}/observation-consent`).then(result<ObservationConsent>),
  extractionAudit:(id:string,payload:ExtractionAuditRequest)=>fetch(`${API}/browser/${id}/extraction-audit`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(result<ExtractionAuditResult>),
  setObservationConsent:(id:string,enabled:boolean,context_token:string)=>fetch(`${API}/browser/${id}/observation-consent`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled,context_token})}).then(result<ObservationConsent>),
  observeRegion:(id:string,selector:string,include_image:boolean,context_token:string)=>fetch(`${API}/browser/${id}/observe-region`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selector,include_image,context_token})}).then(result<PageRegionObservation>),
  selectTaskResume:(id:string,resume_id:string)=>fetch(`${API}/browser/${id}/resume`,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({resume_id})}).then(result<{resume_id:string;context_token:string}>),
  expandBrowserSection:(id:string,selector:string,candidate_id='')=>fetch(`${API}/browser/${id}/expand`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selector,candidate_id})}).then(result<BrowserSnapshot>),
  inspectBrowserField:(id:string,selector:string)=>fetch(`${API}/browser/${id}/field/inspect`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({selector})}).then(result<FormReviewResult>),
  importResumeWithSite:(id:string,resume_id:string,confirm_site_parse=false)=>fetch(`${API}/browser/${id}/native-resume`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resume_id,confirm_site_parse})}).then(result<NativeResumeImportResult>),
  workflowState:(id:string)=>fetch(`${API}/browser/${id}/workflow`).then(result<ApplicationWorkflowState>),
  assessApplicationAgent:(id:string)=>fetch(`${API}/browser/${id}/agent/assess`,{method:'POST'}).then(result<ApplicationAgentTurn>),
  runApplicationAgentStep:(id:string,resume_id:string)=>fetch(`${API}/browser/${id}/agent/step`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resume_id})}).then(result<ApplicationAgentTurn>),
  runApplicationJourney:(id:string,resume_id:string)=>fetch(`${API}/browser/${id}/journey`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({resume_id,max_steps:4})}).then(result<ApplicationJourneyResult>),
  advanceWorkflow:(id:string,intent:'browse_jobs'|'search_jobs'|'open_job'|'start_application'|'create_account'|'continue_application'|'refresh',candidate_id?:string)=>fetch(`${API}/browser/${id}/workflow/advance`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({intent,candidate_id})}).then(result<ApplicationWorkflowState>),
  fillRegistration:(id:string,email:string,phone:string,password:string)=>fetch(`${API}/browser/${id}/workflow/registration`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({email,phone,password})}).then(result<ApplicationWorkflowState>),
  requestVerificationCode:(id:string,channel:'phone'|'email',value='')=>fetch(`${API}/browser/${id}/workflow/request-code`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel,value})}).then(result<ApplicationWorkflowState>),
  enterVerificationCode:(id:string,code:string,submit:boolean)=>fetch(`${API}/browser/${id}/workflow/verification`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code,submit})}).then(result<ApplicationWorkflowState>),
  planForm:(id:string)=>fetch(`${API}/browser/${id}/plan`,{method:'POST'}).then(result<FormPlan>),
  reviewForm:(id:string,useModel=false)=>fetch(`${API}/browser/${id}/review${useModel?'?use_model=true':''}`,{method:'POST'}).then(result<FormReviewResult>),
  autofillPhase:(id:string,phase:'rules'|'model')=>fetch(`${API}/browser/${id}/autofill`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({phase})}).then(result<AutofillPhaseResult>),
  assistApplication:(id:string,payload:ApplicationAssistRequest,runId?:string)=>fetch(`${API}/browser/${id}/assist${runId?`?run_id=${encodeURIComponent(runId)}`:''}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(result<ApplicationAssistResult>),
  assistProgress:(id:string,runId:string)=>fetch(`${API}/browser/${id}/assist/progress/${encodeURIComponent(runId)}`).then(result<ApplicationAssistProgress>),
  cancelAssist:(id:string,runId:string)=>fetch(`${API}/browser/${id}/assist/progress/${encodeURIComponent(runId)}/cancel`,{method:'POST'}).then(result<ApplicationAssistProgress>),
  executeForm:(id:string,actions:FillAction[],resume_id:string,context_token='')=>fetch(`${API}/browser/${id}/execute`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({actions,min_confidence:.85,resume_id,context_token})}).then(result<ExecutionResult>),
  uploadResumeAttachment:(id:string,resumeId:string,contextToken:string,confirmed=false)=>fetch(`${API}/browser/${id}/execute`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(resumeAttachmentRequest(resumeId,contextToken,confirmed))}).then(result<ExecutionResult>),
  checkForm:(id:string)=>fetch(`${API}/browser/${id}/check`).then(result<PreSubmitCheck>),
  closeBrowser:(id:string)=>fetch(`${API}/browser/${id}`,{method:'DELETE'}).then(result<void>),
}
