export type Experience = { organization:string; department:string; role:string; employment_type:string; location:string; start_date:string; end_date:string; current:boolean; description:string; achievements:string[]; technologies:string[] }
export type Project = { name:string; role:string; start_date:string; end_date:string; background:string; description:string; achievements:string[]; technologies:string[]; project_url:string; github_url:string }
export type Education = { school:string; college:string; degree:string; major:string; start_date:string; end_date:string; gpa:string; ranking:string; courses:string[]; description:string; current:boolean }
export type ResumeProfile = {
  name:string; english_name:string; gender:'男'|'女'|'其他'|'未识别'; birth_date:string; age:number|null;
  phone:string; email:string; wechat:string; location:string; hometown:string; website:string; github:string; linkedin:string;
  target_role:string; target_industries:string[]; target_cities:string[]; available_date:string; internship_duration:string;
  days_per_week:string; expected_salary:string; remote_preference:string; summary:string;
  education:Education[]; internships:Experience[]; projects:Project[]; skills:string[]; languages:string[]; certificates:string[]; awards:string[];
}
export type CandidateProfile = ResumeProfile & { id:string; created_at:string|null; updated_at:string|null }
export type Evidence = { id:string; field_path:string; value:unknown; confidence:number; source_text:string; source_page:number|null; status:'pending_review'|'confirmed'|'edited'|'rejected' }
export type ResumeRecord = { id:string; filename:string; label:string; profile:ResumeProfile; parser:string; status:string; language:string; tags:string[]; target_role:string; is_default:boolean; file_size:number; content_hash:string; error_message:string; evidence:Evidence[]; created_at:string; updated_at:string }
export type Conflict = { id:string; field_path:string; current_value:unknown; incoming_value:unknown; resume_id:string; resume_label:string; status:string; resolution:unknown; created_at:string }
export type PageField = { selector:string; label:string; name:string; field_type:string; required:boolean; options:string[]; current_value:string; accept:string }
export type BrowserSnapshot = { session_id:string; url:string; title:string; fields:PageField[] }
export type WorkflowStage = 'job_detail'|'auth_required'|'verification_required'|'profile_form'|'application_form'|'review'|'unknown'
export type WorkflowAction = { intent:'start_application'|'manual_login'|'request_phone_code'|'request_email_code'|'enter_verification'|'analyze_form'|'refresh'; label:string; automated:boolean; requires_user:boolean }
export type ApplicationWorkflowState = { session_id:string; url:string; title:string; adapter:string; stage:WorkflowStage; message:string; job_title:string; job_id:string; authentication_methods:string[]; requires_consent:boolean; verification_channel:'sms'|'email'|'unknown'|''; form_fields:number; final_submit_present:boolean; actions:WorkflowAction[] }
export type FillAction = { selector:string; label:string; action:'fill'|'select'|'check'|'skip'|'ask_user'; value:string|boolean; value_source:string; confidence:number; reason:string; sensitive:boolean; user_confirmed:boolean }
export type FormPlan = { page_summary:string; site_type:string; actions:FillAction[]; missing_questions:string[] }
export type RequiredFieldIssue = { selector:string; label:string; field_type:string }
export type PreSubmitCheck = { url:string; ready:boolean; required_total:number; filled_count:number; required_missing:RequiredFieldIssue[]; validation_errors:string[]; human_challenges:string[]; file_uploads:string[]; submit_labels:string[] }
export type ExecutionResult = { url:string; completed:number; skipped:number; failed:number; verified:number; unverified:number; results:{selector:string;label:string;status:string;message:string;verified:boolean;actual_value:string}[]; pre_submit:PreSubmitCheck }
