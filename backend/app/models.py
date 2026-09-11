from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


ReviewStatus = Literal["pending_review", "confirmed", "edited", "rejected"]


class ModelHealth(BaseModel):
    status: Literal["ok", "unavailable", "misconfigured"]
    model: str = ""
    latency_ms: int = 0
    message: str = ""


class Experience(BaseModel):
    organization: str = ""
    department: str = ""
    role: str = ""
    employment_type: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    current: bool = False
    description: str = ""
    achievements: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)


class Project(BaseModel):
    name: str = ""
    role: str = ""
    start_date: str = ""
    end_date: str = ""
    background: str = ""
    description: str = ""
    achievements: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    project_url: str = ""
    github_url: str = ""


class Education(BaseModel):
    school: str = ""
    college: str = ""
    degree: str = ""
    major: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    gpa: str = ""
    ranking: str = ""
    courses: list[str] = Field(default_factory=list)
    description: str = ""
    current: bool = False


class ResumeProfile(BaseModel):
    name: str = ""
    english_name: str = ""
    gender: Literal["男", "女", "其他", "未识别"] = "未识别"
    birth_date: str = ""
    age: int | None = None
    phone: str = ""
    email: str = ""
    country_region: str = ""
    qq: str = ""
    wechat: str = ""
    location: str = ""
    hometown: str = ""
    website: str = ""
    github: str = ""
    linkedin: str = ""
    target_role: str = ""
    target_industries: list[str] = Field(default_factory=list)
    target_cities: list[str] = Field(default_factory=list)
    available_date: str = ""
    internship_duration: str = ""
    days_per_week: str = ""
    expected_salary: str = ""
    remote_preference: str = ""
    summary: str = ""
    education: list[Education] = Field(default_factory=list)
    internships: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    certificates: list[str] = Field(default_factory=list)
    awards: list[str] = Field(default_factory=list)
    application_answers: dict[str, str] = Field(default_factory=dict)


class CandidateProfile(ResumeProfile):
    id: str = "default"
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ApplicationAnswerUpdate(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    field_name: str = Field(default="", max_length=500)
    value: str = Field(min_length=1, max_length=5000)


class FieldEvidence(BaseModel):
    id: str
    field_path: str
    value: Any
    confidence: float = Field(ge=0, le=1)
    source_text: str
    source_page: int | None = None
    status: ReviewStatus = "pending_review"


class ResumeRecord(BaseModel):
    id: str
    filename: str
    label: str
    profile: ResumeProfile
    parser: str
    status: Literal["pending", "parsing", "completed", "needs_review", "failed"] = "needs_review"
    language: Literal["中文", "英文", "中英混合", "未识别"] = "未识别"
    tags: list[str] = Field(default_factory=list)
    target_role: str = ""
    is_default: bool = False
    file_size: int = 0
    content_hash: str = ""
    error_message: str = ""
    evidence: list[FieldEvidence] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ResumeUpdate(BaseModel):
    label: str | None = None
    profile: ResumeProfile | None = None
    language: Literal["中文", "英文", "中英混合", "未识别"] | None = None
    tags: list[str] | None = None
    target_role: str | None = None
    is_default: bool | None = None


class ReviewUpdate(BaseModel):
    status: ReviewStatus
    value: Any | None = None


class ProfileConflict(BaseModel):
    id: str
    field_path: str
    current_value: Any
    incoming_value: Any
    resume_id: str
    resume_label: str = ""
    status: Literal["pending", "resolved"] = "pending"
    resolution: Any | None = None
    created_at: datetime


class ConflictResolution(BaseModel):
    choice: Literal["current", "incoming", "custom"]
    custom_value: Any | None = None


class ExportBundle(BaseModel):
    exported_at: datetime
    profile: CandidateProfile
    resumes: list[ResumeRecord]
    conflicts: list[ProfileConflict]
