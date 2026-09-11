export type Experience = { organization:string; department:string; role:string; employment_type:string; location:string; start_date:string; end_date:string; current:boolean; description:string; achievements:string[]; technologies:string[] }
export type Project = { name:string; role:string; start_date:string; end_date:string; background:string; description:string; achievements:string[]; technologies:string[]; project_url:string; github_url:string }
export type Education = { school:string; college:string; degree:string; major:string; location:string; start_date:string; end_date:string; gpa:string; ranking:string; courses:string[]; description:string; current:boolean }
export type ResumeProfile = {
  name:string; english_name:string; gender:'男'|'女'|'其他'|'未识别'; birth_date:string; age:number|null;
  phone:string; email:string; country_region:string; qq:string; wechat:string; location:string; hometown:string; website:string; github:string; linkedin:string;
  target_role:string; target_industries:string[]; target_cities:string[]; available_date:string; internship_duration:string;
  days_per_week:string; expected_salary:string; remote_preference:string; summary:string;
  education:Education[]; internships:Experience[]; projects:Project[]; skills:string[]; languages:string[]; certificates:string[]; awards:string[]; application_answers:Record<string,string>;
}
export type CandidateProfile = ResumeProfile & { id:string; created_at:string|null; updated_at:string|null }
export type UserAccount = { id:string; email:string; display_name:string; created_at:string; is_local:boolean }
export type AuthSession = { user:UserAccount }
export type Evidence = { id:string; field_path:string; value:unknown; confidence:number; source_text:string; source_page:number|null; status:'pending_review'|'confirmed'|'edited'|'rejected' }
export type ResumeRecord = { id:string; filename:string; label:string; profile:ResumeProfile; parser:string; status:string; language:string; tags:string[]; target_role:string; is_default:boolean; file_size:number; content_hash:string; error_message:string; evidence:Evidence[]; created_at:string; updated_at:string }
export type Conflict = { id:string; field_path:string; current_value:unknown; incoming_value:unknown; resume_id:string; resume_label:string; status:string; resolution:unknown; created_at:string }
export type PageField = {
  selector:string; label:string; name:string; field_type:string; required:boolean; options:string[];
  current_value:string; accept:string; role:string; group_label:string; option_label:string;
  option_value:string; multiple:boolean; readonly:boolean; section:string;
}
export type BrowserSnapshot = { session_id:string; url:string; title:string; fields:PageField[] }
export type WorkflowStage = 'job_detail'|'registration_required'|'auth_required'|'verification_required'|'profile_form'|'application_form'|'review'|'unknown'
export type WorkflowAction = { intent:'start_application'|'fill_registration'|'create_account'|'manual_login'|'request_phone_code'|'request_email_code'|'enter_verification'|'analyze_form'|'continue_application'|'refresh'; label:string; automated:boolean; requires_user:boolean }
export type ApplicationWorkflowState = { session_id:string; url:string; title:string; adapter:string; stage:WorkflowStage; message:string; job_title:string; job_id:string; authentication_methods:string[]; requires_consent:boolean; verification_channel:'sms'|'email'|'unknown'|''; registration_identifiers:('email'|'phone')[]; registration_requires_password:boolean; form_fields:number; final_submit_present:boolean; safe_next_present:boolean; safe_next_label:string; page_step_current:number|null; page_step_total:number|null; actions:WorkflowAction[] }
export type FillAction = { selector:string; label:string; action:'fill'|'select'|'check'|'skip'|'ask_user'; value:string|boolean; value_source:string; confidence:number; reason:string; sensitive:boolean; user_confirmed:boolean }
export type FormPlan = { page_summary:string; site_type:string; actions:FillAction[]; missing_questions:string[] }
export type ComparisonStatus = 'matched'|'missing'|'conflict'|'manual_review'|'unmapped'|'option_unavailable'
export type FieldComparison = { key:string;selector:string;label:string;field_type:string;required:boolean;options:string[];site_value:string;expected_value:string;value_source:string;status:ComparisonStatus;recommendation:string }
export type ComparisonSummary = { matched:number;missing:number;conflict:number;manual_review:number;unmapped:number;option_unavailable:number }
export type FormReviewResult = { snapshot:BrowserSnapshot;plan:FormPlan;comparisons:FieldComparison[];summary:ComparisonSummary }
export type NativeResumeImportResult = { snapshot:BrowserSnapshot;uploaded_file:string;trigger_clicked:boolean;trigger_label:string;changed_fields:number;status:'parsed'|'uploaded'|'needs_user_action';message:string }
export type RequiredFieldIssue = { selector:string; label:string; field_type:string }
export type PreSubmitCheck = { url:string; ready:boolean; required_total:number; filled_count:number; required_missing:RequiredFieldIssue[]; validation_errors:string[]; human_challenges:string[]; file_uploads:string[]; submit_labels:string[] }
export type ExecutionResult = { url:string; completed:number; skipped:number; failed:number; verified:number; unverified:number; results:{selector:string;label:string;status:string;message:string;verified:boolean;actual_value:string}[]; pre_submit:PreSubmitCheck }
export type LiveJobStatus = 'not_checked'|'open'|'closed'|'manual_gate'|'mismatch'|'unreachable'
export type JobPosting = { id:string; company:string; title:string; job_code:string; locations:string[]; recruitment_type:string; graduation_window:string; education_requirement:string; description:string; required_skills:string[]; preferred_skills:string[]; role_keywords:string[]; url:string; source_name:string; source_url:string; source_status:'verified'|'verify_on_open'; apply_mode:'direct'|'search'; verified_at:string; live_status:LiveJobStatus; last_checked_at:string; verification_message:string; verification_evidence:string[]; discovery_source:string; discovered_at:string; source_updated_at:string; discovery_scope:string; discovery_evidence:string[] }
export type JobRecommendation = { job:JobPosting; match_score:number; matched_skills:string[]; missing_skills:string[]; reasons:string[]; location_match:boolean|null; graduation_match:boolean|null; queue_track:'steady'|'stretch'; formal_queue_eligible:boolean; gate_reasons:string[] }
export type JobVerification = { job_id:string; status:LiveJobStatus; checked_at:string; official_url:string; final_url:string; http_status:number|null; page_title:string; evidence:string[]; message:string; can_proceed:boolean }
export type DiscoverySyncStatus = 'never'|'success'|'partial'|'failed'
export type OfficialJobSource = { id:string; name:string; company:string; official_url:string; enabled:boolean; coverage:string; last_status:DiscoverySyncStatus; last_completed_at:string; last_message:string; jobs_seen:number; total_available:number|null; partial:boolean }
export type JobDiscoveryResult = { source_id:string; source_name:string; status:DiscoverySyncStatus; started_at:string; completed_at:string; jobs_seen:number; created:number; updated:number; skipped:number; total_available:number|null; partial:boolean; message:string; jobs:JobPosting[] }
export type RecommendationBatch = { generated_at:string; engine:string; profile_summary:string; available_locations:string[]; selected_location:string; jobs:JobRecommendation[] }
export type ApplicationQueueItem = { id:string; job_id:string; resume_id:string; status:'planned'|'in_progress'|'needs_review'; created_at:string; updated_at:string; recommendation:JobRecommendation }
