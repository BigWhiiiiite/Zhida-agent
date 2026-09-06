from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class BrowserStart(BaseModel):
    url: str


class PageField(BaseModel):
    selector: str
    label: str = ""
    name: str = ""
    field_type: str = "text"
    required: bool = False
    options: list[str] = Field(default_factory=list)
    current_value: str = ""


class BrowserSnapshot(BaseModel):
    session_id: str
    url: str
    title: str
    fields: list[PageField]


class FillAction(BaseModel):
    selector: str
    label: str
    action: Literal["fill", "select", "check", "skip", "ask_user"]
    value: Any = ""
    value_source: str = ""
    confidence: float = Field(default=0, ge=0, le=1)
    reason: str = ""
    sensitive: bool = False


class FormPlan(BaseModel):
    page_summary: str = ""
    site_type: str = "generic"
    actions: list[FillAction] = Field(default_factory=list)
    missing_questions: list[str] = Field(default_factory=list)


class ExecutePlanRequest(BaseModel):
    actions: list[FillAction]
    min_confidence: float = Field(default=0.85, ge=0, le=1)


class ActionResult(BaseModel):
    selector: str
    label: str
    status: Literal["filled", "skipped", "failed"]
    message: str = ""


class ExecutionResult(BaseModel):
    url: str
    completed: int
    skipped: int
    failed: int
    results: list[ActionResult]
