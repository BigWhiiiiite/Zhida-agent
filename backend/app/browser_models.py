from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .ats_registry import SiteRoute


class ApplicationTarget(BaseModel):
    company: str = Field(default="", max_length=200)
    job_title: str = Field(default="", max_length=200)
    city: str = Field(default="", max_length=100)
    recruitment_cycle: str = Field(default="", max_length=100)
    source_url: str = Field(default="", max_length=2000)


class NavigationCandidate(BaseModel):
    id: str
    label: str
    url: str = ""
    kind: Literal["browse_jobs", "search_jobs", "open_job"]
    matches_target: bool = False
    entry_scope: Literal["navigation", "organization"] = "navigation"
    requires_user_choice: bool = False


class BrowserStart(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(min_length=1, max_length=2000)
    target: ApplicationTarget | None = None
    resume_id: str = Field(default="", max_length=200)
    safari_window_token: str = Field(default="", max_length=200)


class TaskResumeUpdate(BaseModel):
    resume_id: str = Field(default="", max_length=200)


class ExpandSectionRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=1000)
    candidate_id: str = Field(default="", max_length=100)


class NativeResumeImportRequest(BaseModel):
    resume_id: str = Field(min_length=1, max_length=200)
    confirm_site_parse: bool = False


class QuestionEvidence(BaseModel):
    text: str = Field(max_length=500)
    source: str = Field(max_length=40)
    owned: bool = False


class FieldConstraints(BaseModel):
    input_type: str = ""
    input_mode: str = ""
    pattern: str = ""
    min_length: int | None = None
    max_length: int | None = None
    minimum: str = ""
    maximum: str = ""
    step: str = ""


class FieldObservation(BaseModel):
    version: int = 1
    question_status: Literal["verified", "unverified", "missing", "ambiguous"] = "missing"
    options_status: Literal["not_applicable", "native_complete", "group_complete", "observed_subset",
                            "unavailable", "deferred", "dependent", "calendar"] = "not_applicable"
    required_status: Literal["required", "not_marked"] = "not_marked"
    required_evidence: list[str] = Field(default_factory=list)
    record_status: Literal["not_applicable", "container_observed", "unresolved", "ambiguous"] = "not_applicable"
    issues: list[str] = Field(default_factory=list)


class CascadeOptionObservation(BaseModel):
    model_config = {"extra": "forbid"}
    text: str = Field(max_length=240)
    text_truncated: bool = False
    disabled: bool = False
    branch: bool = False


class CascadeLayerObservation(BaseModel):
    model_config = {"extra": "forbid"}
    visible_layer_index: int = Field(ge=0, le=4)
    declared_level: int | None = Field(default=None, ge=1, le=99)
    visible_option_count: int = Field(ge=0)
    truncated: bool = False
    options: list[CascadeOptionObservation] = Field(default_factory=list, max_length=80)


class CascadeObservation(BaseModel):
    """Read evidence of visible columns, never a selected or complete path."""
    model_config = {"extra": "forbid"}
    control_kind: Literal["cascade"] = "cascade"
    read_only: Literal[True] = True
    scope: Literal["owned_current_visible_layers"] = "owned_current_visible_layers"
    options_capture: Literal["dependent"] = "dependent"
    layers: list[CascadeLayerObservation] = Field(default_factory=list, max_length=5)
    observed_layer_count: int = Field(ge=0)
    truncated: bool = False
    complete: Literal[False] = False
    limitations: list[str] = Field(default_factory=list, max_length=5)


class ExtractionIssue(BaseModel):
    label: str
    reason: str
    selector: str = ""


class FormExtractionReport(BaseModel):
    version: int = 1
    scope: Literal["current_visible_document"] = "current_visible_document"
    # observed is not a claim that hidden/conditional fields do not exist.
    capture_status: Literal["observed", "partial", "unknown"] = "unknown"
    observed_controls: int = 0
    captured_controls: int = 0
    intentionally_excluded_controls: int = 0
    unmapped_controls: int = 0
    question_count: int = 0
    verified_questions: int = 0
    unclear_questions: int = 0
    options_pending_questions: int = 0
    ambiguous_record_questions: int = 0
    embedded_regions: int = 0
    unread_shadow_regions: int = 0
    pending_sections: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    issues: list[ExtractionIssue] = Field(default_factory=list)


class PageField(BaseModel):
    selector: str
    label: str = ""
    # Exact user-facing question reconstructed from DOM evidence. `label` remains
    # for backwards compatibility, while this is the preferred review title.
    question_text: str = ""
    label_source: str = "unknown"
    context: str = ""
    help_text: str = ""
    nearby_labels: list[str] = Field(default_factory=list)
    section_path: list[str] = Field(default_factory=list)
    recognition_confidence: float = Field(default=0, ge=0, le=1)
    placeholder: str = ""
    ordinal: int = 0
    name: str = ""
    field_type: str = "text"
    required: bool = False
    options: list[str] = Field(default_factory=list)
    current_value: str = ""
    accept: str = ""
    role: str = ""
    section: str = ""
    group_label: str = ""
    option_label: str = ""
    option_value: str = ""
    multiple: bool = False
    readonly: bool = False
    autocomplete: str = ""
    # Stable semantic metadata is separated from the temporary DOM selector.
    semantic_key: str = ""
    entity_scope: str = ""
    field_signature: str = ""
    signature_rank: int = 0
    container_key: str = ""
    record_evidence: str = ""
    record_keys: list[str] = Field(default_factory=list)
    # All options belonging to one radio/checkbox question share this DOM-derived key.
    # It prevents option captions such as “是” and “否” from becoming separate questions.
    control_group_key: str = ""
    expected_input: str = ""
    date_precision: Literal["", "date", "month"] = ""
    control_kind: str = ""
    control_evidence: str = ""
    # Proven from this control's own opened area menu, never label guessing.
    region_picker: bool = False
    region_value_path: str = ""
    recognition_evidence: str = ""
    # Raw, control-owned evidence is retained separately from a heuristic title.
    question_candidates: list[QuestionEvidence] = Field(default_factory=list)
    constraints: FieldConstraints = Field(default_factory=FieldConstraints)
    observation: FieldObservation | None = None
    required_evidence: list[str] = Field(default_factory=list)
    options_capture: str = ""
    cascade_observation: CascadeObservation | None = None
    # Server-derived knowledge references, never personal values or executable instructions.
    knowledge_profile_path: str = ""
    knowledge_id: str = ""
    knowledge_block_reason: str = ""


class FieldKnowledgeEvidence(BaseModel):
    selector: str
    knowledge_id: str
    kind: Literal["mapping", "rule"]
    question: str
    profile_path: str = ""
    semantic_key: str = ""
    entity_scope: str = ""
    note: str = ""
    score: float = 0
    exact: bool = False
    usable: bool = False
    reason: str = ""
    retrieval_mode: Literal["hybrid", "keyword-only"] = "keyword-only"


class BrowserSnapshot(BaseModel):
    session_id: str
    url: str
    title: str
    browser_engine: Literal["safari", "chromium"] = "chromium"
    recognition_profile: str = "generic-semantic"
    site_route: SiteRoute = Field(default_factory=SiteRoute)
    fields: list[PageField]
    # Prompt-only neighbour/record evidence. Not executable action targets.
    context_fields: list[PageField] = Field(default_factory=list)
    knowledge_context: list[FieldKnowledgeEvidence] = Field(default_factory=list)
    extraction_report: FormExtractionReport | None = None


class FillAction(BaseModel):
    selector: str
    label: str
    action: Literal["fill", "select", "check", "skip", "ask_user"]
    # Browser actions only need text for inputs/selects or a boolean for checks.
    # Keeping this concrete also produces a valid strict JSON Schema for models
    # using structured outputs (an unconstrained Any has no schema `type`).
    value: str | bool = ""
    value_source: str = ""
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = ""
    sensitive: bool = False
    user_confirmed: bool = False
    profile_path: str = ""
    entity_scope: str = ""
    question_evidence: str = ""
    # Routing metadata is assigned by the server, never trusted from the LLM.
    resolution_source: Literal["rules", "model", "user", "blocked"] = "rules"
    needs_model: bool = False
    review_question: str = ""
    review_hint: str = ""


class FormRoutingSummary(BaseModel):
    rules_ready: int = 0
    model_resolved: int = 0
    needs_user: int = 0
    model_pending: int = 0


class FormPlan(BaseModel):
    context_token: str = ""
    resume_id: str = ""
    page_summary: str = ""
    site_type: str = "generic"
    actions: list[FillAction] = Field(default_factory=list)
    missing_questions: list[str] = Field(default_factory=list)
    knowledge_matches: list[FieldKnowledgeEvidence] = Field(default_factory=list)
    routing_summary: FormRoutingSummary = Field(default_factory=FormRoutingSummary)


class FieldComparison(BaseModel):
    key: str
    selector: str
    label: str
    field_type: str
    required: bool = False
    options: list[str] = Field(default_factory=list)
    site_value: str = ""
    expected_value: str = ""
    value_source: str = ""
    status: Literal["matched", "missing", "conflict", "manual_review", "unmapped", "option_unavailable"]
    recommendation: str = ""


class ComparisonSummary(BaseModel):
    matched: int = 0
    missing: int = 0
    conflict: int = 0
    manual_review: int = 0
    unmapped: int = 0
    option_unavailable: int = 0


class FormReviewResult(BaseModel):
    snapshot: BrowserSnapshot
    plan: FormPlan
    comparisons: list[FieldComparison] = Field(default_factory=list)
    summary: ComparisonSummary = Field(default_factory=ComparisonSummary)


class NativeResumeImportResult(BaseModel):
    snapshot: BrowserSnapshot
    uploaded_file: str
    trigger_clicked: bool = False
    trigger_label: str = ""
    changed_fields: int = 0
    status: Literal["parsed", "uploaded", "needs_user_action"] = "uploaded"
    message: str = ""


class ExecutePlanRequest(BaseModel):
    context_token: str = ""
    actions: list[FillAction]
    min_confidence: float = Field(default=0.85, ge=0, le=1)
    resume_id: str = ""
    # Binding the task CV is not permission to re-upload it on every edit.
    upload_resume: bool = False


class ActionResult(BaseModel):
    selector: str
    label: str
    status: Literal["filled", "skipped", "failed"]
    message: str = ""
    verified: bool = False
    actual_value: str = ""


class RequiredFieldIssue(BaseModel):
    selector: str
    label: str
    field_type: str


class PreSubmitCheck(BaseModel):
    url: str
    ready: bool = False
    required_total: int = 0
    filled_count: int = 0
    required_missing: list[RequiredFieldIssue] = Field(default_factory=list)
    validation_errors: list[str] = Field(default_factory=list)
    human_challenges: list[str] = Field(default_factory=list)
    file_uploads: list[str] = Field(default_factory=list)
    submit_labels: list[str] = Field(default_factory=list)


class ExecutionResult(BaseModel):
    url: str
    completed: int
    skipped: int
    failed: int
    verified: int = 0
    unverified: int = 0
    results: list[ActionResult]
    pre_submit: PreSubmitCheck


class HybridAutofillRequest(BaseModel):
    model_config = {"extra": "forbid"}
    phase: Literal["rules", "model"] = "rules"


class HybridAutofillResult(BaseModel):
    phase: Literal["rules", "model"]
    review: FormReviewResult
    execution: ExecutionResult


class ApplicationAssistRequest(BaseModel):
    model_config = {"extra": "forbid"}
    resume_id: str = Field(min_length=1, max_length=200)
    allow_site_parse: bool = False
    use_model: bool = True
    max_rounds: int = Field(default=3, ge=1, le=5)
    # Explicit per-run deferrals; never a value or a replacement for required checks.
    defer_government_id: bool = False
    deferred_fields: list[PageField] = Field(default_factory=list, max_length=50)


class ApplicationRecordCoverage(BaseModel):
    kind: Literal["education", "internships", "projects"]
    label: str
    source_total: int
    website_records: int
    matched_records: int
    missing_names: list[str] = Field(default_factory=list)
    ambiguous: bool = False
    can_expand: bool = False


class ApplicationAssistIssue(BaseModel):
    label: str
    message: str


class ApplicationAssistEvent(BaseModel):
    kind: str
    message: str
    completed: int = 0
    failed: int = 0
    issues: list[ApplicationAssistIssue] = Field(default_factory=list)


class ApplicationAssistResult(BaseModel):
    status: Literal["ready_for_review", "needs_user", "blocked", "partial"]
    message: str
    snapshot: BrowserSnapshot
    review: FormReviewResult | None = None
    pre_submit: PreSubmitCheck | None = None
    events: list[ApplicationAssistEvent] = Field(default_factory=list)
    rounds: int = 0
    record_coverage: list[ApplicationRecordCoverage] = Field(default_factory=list)
    model_calls: int = 0


class ApplicationAssistProgress(BaseModel):
    run_id: str
    status: Literal['running', 'finished', 'interrupted'] = 'running'
    phase: str = 'observe'
    message: str = '正在读取当前申请表'
    events: list[ApplicationAssistEvent] = Field(default_factory=list)
    result: ApplicationAssistResult | None = None
    cancel_requested: bool = False
