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


class BrowserStart(BaseModel):
    model_config = {"extra": "forbid"}
    url: str = Field(min_length=1, max_length=2000)
    target: ApplicationTarget | None = None


class ExpandSectionRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=1000)


class NativeResumeImportRequest(BaseModel):
    resume_id: str = Field(min_length=1, max_length=200)


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
    # All options belonging to one radio/checkbox question share this DOM-derived key.
    # It prevents option captions such as “是” and “否” from becoming separate questions.
    control_group_key: str = ""
    expected_input: str = ""
    recognition_evidence: str = ""
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
    recognition_profile: str = "generic-semantic"
    site_route: SiteRoute = Field(default_factory=SiteRoute)
    fields: list[PageField]
    knowledge_context: list[FieldKnowledgeEvidence] = Field(default_factory=list)


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
    actions: list[FillAction]
    min_confidence: float = Field(default=0.85, ge=0, le=1)
    resume_id: str = ""


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
