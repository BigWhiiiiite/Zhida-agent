from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, SecretStr


ApplicationStage = Literal[
    "job_detail", "registration_required", "auth_required", "verification_required",
    "profile_form", "application_form", "review", "unknown",
]


class WorkflowAction(BaseModel):
    intent: Literal[
        "start_application", "fill_registration", "create_account", "manual_login",
        "request_phone_code", "request_email_code", "enter_verification",
        "analyze_form", "continue_application", "refresh",
    ]
    label: str
    automated: bool = False
    requires_user: bool = False


class ApplicationWorkflowState(BaseModel):
    session_id: str
    url: str
    title: str
    adapter: str = "generic"
    stage: ApplicationStage = "unknown"
    message: str = ""
    job_title: str = ""
    job_id: str = ""
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


class WorkflowAdvanceRequest(BaseModel):
    intent: Literal["start_application", "create_account", "continue_application", "refresh"]


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
