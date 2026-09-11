from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class BrowserStart(BaseModel):
    url: str


class ExpandSectionRequest(BaseModel):
    selector: str = Field(min_length=1, max_length=1000)


class NativeResumeImportRequest(BaseModel):
    resume_id: str = Field(min_length=1, max_length=200)


class PageField(BaseModel):
    selector: str
    label: str = ""
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


class BrowserSnapshot(BaseModel):
    session_id: str
    url: str
    title: str
    fields: list[PageField]


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


class FormPlan(BaseModel):
    page_summary: str = ""
    site_type: str = "generic"
    actions: list[FillAction] = Field(default_factory=list)
    missing_questions: list[str] = Field(default_factory=list)


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
