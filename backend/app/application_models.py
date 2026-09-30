from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr

from .browser_models import (ApplicationTarget, NavigationCandidate, BrowserSnapshot,
                             ExecutionResult, FormReviewResult, PreSubmitCheck)
from .ats_registry import SiteRoute


ApplicationStage = Literal[
    "homepage", "job_list", "job_detail", "registration_required", "auth_required", "verification_required",
    "profile_form", "application_form", "review", "unknown",
]


class WorkflowAction(BaseModel):
    intent: Literal[
        "start_application", "fill_registration", "create_account", "manual_login",
        "request_phone_code", "request_email_code", "enter_verification",
        "analyze_form", "continue_application", "refresh",
        "browse_jobs", "search_jobs", "open_job",
    ]
    label: str
    automated: bool = False
    requires_user: bool = False


class ApplicationWorkflowState(BaseModel):
    session_id: str
    url: str
    title: str
    adapter: str = "generic"
    site_route: SiteRoute = Field(default_factory=SiteRoute)
    stage: ApplicationStage = "unknown"
    message: str = ""
    job_title: str = ""
    job_id: str = ""
    authenticated: bool = False
    authentication_evidence: list[str] = Field(default_factory=list)
    authentication_methods: list[str] = Field(default_factory=list)
    requires_consent: bool = False
    verification_channel: Literal["sms", "email", "unknown", ""] = ""
    registration_identifiers: list[Literal["email", "phone"]] = Field(default_factory=list)
    registration_requires_password: bool = False
    form_fields: int = 0
    final_submit_present: bool = False
    safe_next_present: bool = False
    safe_next_label: str = ""
    page_step_current: int | None = None
    page_step_total: int | None = None
    actions: list[WorkflowAction] = Field(default_factory=list)
    target: ApplicationTarget = Field(default_factory=ApplicationTarget)
    navigation_candidates: list[NavigationCandidate] = Field(default_factory=list)
    navigation_blocker: str = ""
    stage_evidence: list[str] = Field(default_factory=list)


class WorkflowAdvanceRequest(BaseModel):
    intent: Literal["start_application", "create_account", "continue_application", "refresh",
                    "browse_jobs", "search_jobs", "open_job"]
    candidate_id: str = Field(default="", max_length=100)


class RegistrationCredentialsRequest(BaseModel):
    email: str = Field(default="", max_length=320)
    phone: str = Field(default="", max_length=40)
    password: SecretStr | None = Field(default=None, max_length=128)


class VerificationCodeRequest(BaseModel):
    code: SecretStr = Field(min_length=4, max_length=12)
    submit: bool = False


class VerificationRequest(BaseModel):
    channel: Literal["phone", "email"]
    value: str = Field(default="", max_length=320)


AgentNextAction = Literal[
    "start_application", "analyze_and_fill", "continue_application", "refresh",
    "wait_for_registration", "wait_for_login", "wait_for_verification",
    "review_before_submit", "stop",
    "browse_jobs", "search_jobs", "open_job",
]


class ApplicationAgentDecision(BaseModel):
    """A model-informed recommendation constrained by the workflow state machine."""

    stage: ApplicationStage
    goal: str
    summary: str
    next_action: AgentNextAction
    next_label: str
    rationale: str
    blockers: list[str] = Field(default_factory=list, max_length=12)
    user_questions: list[str] = Field(default_factory=list, max_length=8)
    can_execute: bool = False
    requires_user: bool = False
    risk_level: Literal["low", "medium", "high"] = "low"
    model_status: Literal["model", "local_fallback"] = "local_fallback"
    model: str = ""
    candidate_id: str = ""
    observed_stage: ApplicationStage | Literal[""] = ""
    stage_conflict: bool = False
    stage_conflict_evidence: str = ""


class ApplicationAgentStepRequest(BaseModel):
    resume_id: str = Field(default="", max_length=200)


class ApplicationAgentEvent(BaseModel):
    action: str
    stage: ApplicationStage
    summary: str
    created_at: datetime


class ApplicationAgentCheckpoint(BaseModel):
    run_id: str
    session_id: str
    status: Literal["active", "waiting_user", "review", "completed", "stopped"]
    stage: ApplicationStage
    url: str
    title: str
    updated_at: datetime
    events: list[ApplicationAgentEvent] = Field(default_factory=list)


class ApplicationAgentTurn(BaseModel):
    decision: ApplicationAgentDecision
    workflow: ApplicationWorkflowState
    snapshot: BrowserSnapshot
    action_taken: AgentNextAction | Literal[""] = ""
    review: FormReviewResult | None = None
    execution: ExecutionResult | None = None
    pre_submit: PreSubmitCheck | None = None
    checkpoint: ApplicationAgentCheckpoint
