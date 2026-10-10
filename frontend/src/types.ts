export type Experience = { organization:string; department:string; role:string; employment_type:string; location:string; start_date:string; end_date:string; current:boolean; description:string; achievements:string[]; technologies:string[] }
export type Project = { name:string; role:string; start_date:string; end_date:string; background:string; description:string; responsibilities?:string; achievements:string[]; technologies:string[]; project_url:string; github_url:string }
export type Education = { school:string; college:string; degree:string; major:string; study_mode:string; academic_system:string; student_id:string; advisor:string; laboratory:string; research_direction:string; location:string; start_date:string; end_date:string; gpa:string; ranking:string; courses:string[]; description:string; current:boolean }
export type ApplicationAnswerMemory = { id:string;question:string;normalized_question:string;semantic_key:string;entity_scope:string;field_signature:string;field_type:string;option_fingerprint:string;value:string;source_host:string;confirmed_count:number;updated_at:string }
export type ResumeProfile = {
  name:string; english_name:string; gender:'男'|'女'|'其他'|'未识别'; birth_date:string; age:number|null;
  phone:string; email:string; country_region:string; nationality:string; ethnicity:string; political_status:string; marital_status:string; qq:string; wechat:string; location:string; hometown:string; hukou_location:string; address:string; website:string; github:string; linkedin:string;
  height_cm?:number|null; weight_kg?:number|null; student_origin?:string; veteran_status?:''|'是'|'否'; study_continuity?:''|'是'|'否'; formal_employment_status?:''|'有'|'无';
  target_role:string; target_industries:string[]; target_cities:string[]; preferred_business_groups:string[]; interview_preferences:string[]; willing_to_relocate:string; campus_candidate_type:string; available_date:string; internship_duration:string;
  days_per_week:string; expected_salary:string; remote_preference:string; summary:string;
  education:Education[]; internships:Experience[]; projects:Project[]; skills:string[]; languages:string[]; certificates:string[]; awards:string[]; application_answers:Record<string,string>; application_answer_memory:ApplicationAnswerMemory[];
}
export type CandidateProfile = ResumeProfile & { id:string; created_at:string|null; updated_at:string|null }
export type UserAccount = { id:string; email:string; display_name:string; created_at:string; is_local:boolean }
export type AuthSession = { user:UserAccount }
export type Evidence = { id:string; field_path:string; value:unknown; confidence:number; source_text:string; source_page:number|null; status:'pending_review'|'confirmed'|'edited'|'rejected' }
export type ResumeRecord = { id:string; filename:string; label:string; profile:ResumeProfile; parser:string; status:string; language:string; tags:string[]; target_role:string; is_default:boolean; file_size:number; content_hash:string; error_message:string; evidence:Evidence[]; created_at:string; updated_at:string }
export type ResumeFactTargets = { resume_id:string;revision:string;records:{record_key:string;section:'education'|'internships'|'projects';label:string;attributes:{key:string;label:string;value:string}[]}[];warnings:string[] }
export type ConfirmedResumeFact = { revision:string;record_key:string;attribute:string;value:string;confirmed:true }
export type Conflict = { id:string; field_path:string; current_value:unknown; incoming_value:unknown; resume_id:string; resume_label:string; status:string; resolution:unknown; created_at:string }
export type CascadeObservation = {
  control_kind:'cascade';read_only:true;scope:'owned_current_visible_layers';options_capture:'dependent';
  layers:{visible_layer_index:number;declared_level:number|null;visible_option_count:number;truncated:boolean;
    options:{text:string;text_truncated:boolean;disabled:boolean;branch:boolean}[]}[];
  observed_layer_count:number;truncated:boolean;complete:false;limitations:string[];
}
export type PageField = {
  question_candidates?:{text:string;source:string;owned:boolean}[];
  required_evidence?:string[];options_capture?:string;
  cascade_observation?:CascadeObservation|null;
  constraints?:{input_type:string;input_mode:string;pattern:string;min_length:number|null;max_length:number|null;minimum:string;maximum:string;step:string};
  observation?:{version:number;question_status:'verified'|'unverified'|'missing'|'ambiguous';options_status:'not_applicable'|'native_complete'|'group_complete'|'observed_subset'|'unavailable'|'deferred'|'dependent'|'calendar';required_status:'required'|'not_marked';required_evidence:string[];record_status:'not_applicable'|'container_observed'|'unresolved'|'ambiguous';issues:string[]}|null;
  date_precision?:''|'date'|'month';
  control_kind?:string; control_evidence?:string;
  region_picker?:boolean;region_value_path?:string;
  knowledge_block_reason?:string;
  selector:string; label:string; question_text:string; label_source:string; context:string; help_text:string; nearby_labels:string[]; section_path:string[]; recognition_confidence:number; placeholder:string; ordinal:number; name:string; field_type:string; required:boolean; options:string[];
  current_value:string; accept:string; role:string; group_label:string; option_label:string;
  option_value:string; multiple:boolean; readonly:boolean; autocomplete:string; section:string;
  semantic_key:string; entity_scope:string; field_signature:string; signature_rank:number; container_key:string; record_keys?:string[]; control_group_key:string; expected_input:string; recognition_evidence:string;
}
export type SiteRoute = { adapter:string;label:string;matched_by:'host'|'dom'|'fallback';evidence:string[];tools:string[];note:string }
export type FormExtractionReport = {version:number;scope:'current_visible_document';capture_status:'observed'|'partial'|'unknown';observed_controls:number;captured_controls:number;intentionally_excluded_controls:number;unmapped_controls:number;question_count:number;verified_questions:number;unclear_questions:number;options_pending_questions:number;ambiguous_record_questions:number;embedded_regions:number;unread_shadow_regions:number;pending_sections:string[];limitations:string[];issues:{label:string;reason:string;selector:string}[]}
export type BrowserSnapshot = { session_id:string; url:string; title:string; browser_engine?:'safari'|'chromium'; recognition_profile:string; site_route:SiteRoute; fields:PageField[];extraction_report?:FormExtractionReport|null }
export type ObservationConsent = {enabled:boolean;context_token:string}
export type ExtractionAuditRequest = {context_token:string;use_model:boolean;inspect_controls:boolean;include_images:boolean}
export type ExtractionAuditReview = {question_id:string;interpretation:string;control_kind:string;verdict:'clear'|'needs_observation'|'conflict';issue:string;next_observation:string;evidence:string[]}
export type ExtractionAuditQuestion = {question_id:string;title:string;section_path:string[];selectors:string[];control_kind:string;required_status:string;options_status:string;record_status:string;observed_options:string[];issues:string[];model_review:ExtractionAuditReview|null}
export type ExtractionAuditResult = {session_id:string;read_only:true;scope:'current_visible_document';model_status:'complete'|'partial'|'unavailable'|'not_requested';model_name:string;total_questions:number;model_reviewed_questions:number;model_batches:number;observations_requested:number;observations_received:number;images_supplied:number;input_manifest:Record<string,unknown>;coverage:FormExtractionReport;limitations:string[];questions:ExtractionAuditQuestion[]}
export type PageRegionObservation = {selector:string;scope:string;accessibility_source:string;accessibility:string;context:Record<string,unknown>;image_data_url:string;limitations:string[];read_only:boolean;privacy_note:string}
export type ApplicationTarget = { company:string;job_title:string;city:string;recruitment_cycle:string;source_url:string }
export type NavigationCandidate = { id:string;label:string;url:string;kind:'browse_jobs'|'search_jobs'|'open_job';matches_target:boolean;entry_scope?:'navigation'|'organization';requires_user_choice?:boolean }
export type ChatConversationSummary = { id:string;title:string;created_at:string;updated_at:string }
export type ChatMessage = { id:string;role:'user'|'assistant';content:string;created_at:string;image_names:string[] }
export type ChatTaskDraft = { company:string;job_title:string;city:string;recruitment_cycle:string;url:string;intent:'apply'|'recommend'|'profile'|'clarify';summary:string;warnings:string[];links:{url:string;source:'text'|'qr'|'image';verified:boolean}[];needs_confirmation:boolean }
export type ChatConversation = ChatConversationSummary & {messages:ChatMessage[];draft:ChatTaskDraft|null}
export type WorkflowStage = 'homepage'|'job_list'|'job_detail'|'registration_required'|'auth_required'|'verification_required'|'profile_form'|'application_form'|'review'|'unknown'
export type WorkflowAction = { intent:'browse_jobs'|'search_jobs'|'open_job'|'start_application'|'fill_registration'|'create_account'|'manual_login'|'request_phone_code'|'request_email_code'|'enter_verification'|'analyze_form'|'continue_application'|'refresh'; label:string; automated:boolean; requires_user:boolean }
export type ApplicationWorkflowState = { session_id:string; url:string; title:string; adapter:string; site_route:SiteRoute; stage:WorkflowStage; message:string; job_title:string; job_id:string; authenticated:boolean; authentication_evidence:string[]; authentication_methods:string[]; requires_consent:boolean; verification_channel:'sms'|'email'|'unknown'|''; registration_identifiers:('email'|'phone')[]; registration_requires_password:boolean; form_fields:number; final_submit_present:boolean; safe_next_present:boolean; safe_next_label:string; page_step_current:number|null; page_step_total:number|null; target?:ApplicationTarget;navigation_candidates?:NavigationCandidate[];navigation_blocker?:string;stage_evidence?:string[]; actions:WorkflowAction[] }
export type AgentNextAction = 'browse_jobs'|'search_jobs'|'open_job'|'start_application'|'analyze_and_fill'|'continue_application'|'refresh'|'wait_for_registration'|'wait_for_login'|'wait_for_verification'|'review_before_submit'|'stop'
export type ApplicationAgentDecision = { stage:WorkflowStage;goal:string;summary:string;next_action:AgentNextAction;next_label:string;rationale:string;blockers:string[];user_questions:string[];can_execute:boolean;requires_user:boolean;risk_level:'low'|'medium'|'high';model_status:'model'|'local_fallback';model:string;candidate_id?:string }
export type ApplicationAgentEvent = { action:string;stage:WorkflowStage;summary:string;created_at:string }
export type ApplicationAgentCheckpoint = { run_id:string;session_id:string;status:'active'|'waiting_user'|'review'|'completed'|'stopped';stage:WorkflowStage;url:string;title:string;updated_at:string;events:ApplicationAgentEvent[] }
export type ApplicationAgentTurn = { decision:ApplicationAgentDecision;workflow:ApplicationWorkflowState;snapshot:BrowserSnapshot;action_taken:AgentNextAction|'';review:FormReviewResult|null;execution:ExecutionResult|null;pre_submit:PreSubmitCheck|null;checkpoint:ApplicationAgentCheckpoint;assistance?:ApplicationAssistResult|null }
export type ApplicationJourneyResult = { status:'waiting_login'|'waiting_registration'|'waiting_verification'|'needs_user'|'ready_for_review'|'blocked'|'partial';message:string;turn:ApplicationAgentTurn;steps:number;events:{action:string;stage:WorkflowStage;message:string}[] }
export type FillAction = { selector:string; label:string; action:'fill'|'select'|'check'|'skip'|'ask_user'; value:string|boolean; value_source:string; confidence:number; reason:string; sensitive:boolean; user_confirmed:boolean; resolution_source?:'rules'|'model'|'user'|'blocked'; review_question?:string;review_hint?:string;needs_model?:boolean }
export type KnowledgeMappingTarget = { path:string;label:string;semantic_key:string;requires_entity_scope:boolean }
export type ApplicationKnowledgeInput = { kind:'mapping'|'rule';question:string;aliases?:string[];source_url:string;section?:string;profile_path?:string;entity_scope?:string;field_signature?:string;field_type?:string;note?:string;confirmed:boolean;expires_at?:string|null }
export type ApplicationKnowledgeRecord = ApplicationKnowledgeInput & { id:string;site_scope:string;semantic_key:string;created_at:string;updated_at:string }
export type ApplicationKnowledgeMatch = { selector:string;knowledge_id:string;question:string;profile_path:string;entity_scope:string;score:number;usable:boolean;reason:string;retrieval_mode:string }
export type FormRoutingSummary = { rules_ready:number;model_resolved:number;needs_user:number;model_pending:number }
export type FormPlan = { context_token?:string; resume_id?:string; page_summary:string; site_type:string; actions:FillAction[]; missing_questions:string[]; knowledge_matches?:ApplicationKnowledgeMatch[]; routing_summary?:FormRoutingSummary }
export type ComparisonStatus = 'matched'|'missing'|'conflict'|'manual_review'|'unmapped'|'option_unavailable'
export type FieldComparison = { key:string;selector:string;label:string;field_type:string;required:boolean;options:string[];site_value:string;expected_value:string;value_source:string;status:ComparisonStatus;recommendation:string }
export type ComparisonSummary = { matched:number;missing:number;conflict:number;manual_review:number;unmapped:number;option_unavailable:number }
export type FormReviewResult = { snapshot:BrowserSnapshot;plan:FormPlan;comparisons:FieldComparison[];summary:ComparisonSummary }
export type AutofillPhaseResult = { phase:'rules'|'model';review:FormReviewResult;execution:ExecutionResult }
export type ApplicationAssistRequest = { resume_id:string;allow_site_parse:boolean;use_model:boolean;max_rounds:number;defer_government_id?:boolean;deferred_fields?:PageField[] }
export type ApplicationRecordCoverage = {kind:'education'|'internships'|'projects';label:string;source_total:number;website_records:number;matched_records:number;missing_names:string[];ambiguous:boolean;can_expand:boolean}
export type ApplicationAssistResult = { status:'ready_for_review'|'needs_user'|'blocked'|'partial';message:string;snapshot:BrowserSnapshot;review:FormReviewResult|null;pre_submit:PreSubmitCheck|null;events:{kind:string;message:string;completed:number;failed:number;issues?:{label:string;message:string}[]}[];rounds:number;model_calls?:number;record_coverage?:ApplicationRecordCoverage[] }
export type ApplicationAssistProgress = {run_id:string;status:'running'|'finished'|'interrupted';phase:string;message:string;events:ApplicationAssistResult['events'];result:ApplicationAssistResult|null;cancel_requested:boolean}
export type NativeResumeImportResult = { snapshot:BrowserSnapshot;uploaded_file:string;trigger_clicked:boolean;trigger_label:string;changed_fields:number;status:'parsed'|'uploaded'|'needs_user_action';message:string }
export type RequiredFieldIssue = { selector:string; label:string; field_type:string }
export type PreSubmitCheck = { url:string; ready:boolean; required_total:number; filled_count:number; required_missing:RequiredFieldIssue[]; validation_errors:string[]; human_challenges:string[]; file_uploads:string[]; submit_labels:string[] }
export type ExecutionResult = { url:string; completed:number; skipped:number; failed:number; verified:number; unverified:number; results:{selector:string;label:string;status:string;message:string;verified:boolean;actual_value:string}[]; pre_submit:PreSubmitCheck }
export type LiveJobStatus = 'not_checked'|'open'|'closed'|'manual_gate'|'mismatch'|'unreachable'
export type CompanySize = 'large'|'growth'|'startup'|'unknown'
export type DiscoveryAdapter = 'auto'|'baidu'|'greenhouse'|'lever'|'ashby'|'smartrecruiters'|'jsonld'
export type JobPosting = { id:string; company:string; title:string; job_code:string; locations:string[]; recruitment_type:string; graduation_window:string; education_requirement:string; description:string; required_skills:string[]; preferred_skills:string[]; role_keywords:string[]; url:string; source_name:string; source_url:string; source_status:'verified'|'verify_on_open'; apply_mode:'direct'|'search'; verified_at:string; live_status:LiveJobStatus; last_checked_at:string; verification_message:string; verification_evidence:string[]; discovery_source:string; discovered_at:string; source_updated_at:string; discovery_scope:string; discovery_evidence:string[]; company_size:CompanySize }
export type JobRecommendation = { job:JobPosting; match_score:number; matched_skills:string[]; missing_skills:string[]; reasons:string[]; location_match:boolean|null; graduation_match:boolean|null; queue_track:'steady'|'stretch'; formal_queue_eligible:boolean; gate_reasons:string[]; evidence_matches:JobEvidenceMatch[]; evidence_gaps:string[] }
export type JobEvidenceMatch = { requirement:string;requirement_type:'required'|'preferred';evidence_id:string;source_kind:'project'|'internship';source_title:string;source_path:string;quote:string;support:'direct'|'related';lexical_score:number;semantic_score:number }
export type JobEvidenceExplanation = { job_id:string;status:'model'|'local_fallback'|'no_evidence';model:string;summary:string;supported_reasons:string[];gaps:string[];evidence_ids:string[] }
export type JobVerification = { job_id:string; status:LiveJobStatus; checked_at:string; official_url:string; final_url:string; http_status:number|null; page_title:string; evidence:string[]; message:string; can_proceed:boolean }
export type DiscoverySyncStatus = 'never'|'success'|'partial'|'failed'
export type OfficialJobSource = { id:string; name:string; company:string; official_url:string; adapter:DiscoveryAdapter; source_key:string; company_size:CompanySize; user_added:boolean; enabled:boolean; coverage:string; last_status:DiscoverySyncStatus; last_completed_at:string; last_message:string; jobs_seen:number; total_available:number|null; partial:boolean }
export type JobDiscoveryResult = { source_id:string; source_name:string; status:DiscoverySyncStatus; started_at:string; completed_at:string; jobs_seen:number; created:number; updated:number; skipped:number; total_available:number|null; partial:boolean; message:string; jobs:JobPosting[] }
export type RecommendationBatch = { generated_at:string; engine:string; profile_summary:string; available_locations:string[]; selected_location:string; jobs:JobRecommendation[]; rag_status:'not_run'|'ready'|'keyword_only'|'no_evidence'; rag_model:string; rag_evidence_count:number; rag_enriched_jobs:number; rag_message:string }
export type JobSearchSourceSummary = { source_id:string;source_name:string;status:DiscoverySyncStatus;jobs_seen:number;message:string }
export type SmartJobSearchResult = { query:string;generated_at:string;synced_sources:number;successful_sources:number;failed_sources:number;discovered_jobs:number;sources:JobSearchSourceSummary[];batch:RecommendationBatch }
export type QueueStatus = 'planned'|'in_progress'|'needs_review'|'ready_to_submit'|'submitted'|'interview'|'offer'|'rejected'|'withdrawn'
export type ApplicationQueueItem = { id:string; job_id:string; resume_id:string; status:QueueStatus; notes:string; application_id:string; status_changed_at:string; submitted_at:string|null; assistance_started_at:string|null; confirmation_pending:boolean; created_at:string; updated_at:string; recommendation:JobRecommendation }
export type ApplicationReadiness = { ready:boolean; score:number; blockers:string[]; warnings:string[]; resume_count:number; default_resume_id:string; pending_resume_fields:number; pending_conflicts:number; queued_jobs:number; official_sources_ready:number; official_sources_total:number }
export type ModelHealth = { status:'ok'|'misconfigured'|'unavailable'; model:string; latency_ms:number; message:string }
