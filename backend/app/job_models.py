from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


SourceStatus = Literal["verified", "verify_on_open"]
ApplyMode = Literal["direct", "search"]
QueueTrack = Literal["steady", "stretch"]
QueueStatus = Literal["planned", "in_progress", "needs_review"]


class JobPosting(BaseModel):
    id: str
    company: str
    title: str
    job_code: str = ""
    locations: list[str] = Field(default_factory=list)
    recruitment_type: str = "校园招聘"
    graduation_window: str = ""
    education_requirement: str = ""
    description: str = ""
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    role_keywords: list[str] = Field(default_factory=list)
    url: str
    source_name: str
    source_url: str
    source_status: SourceStatus = "verify_on_open"
    apply_mode: ApplyMode = "direct"
    verified_at: str = ""


class JobRecommendation(BaseModel):
    job: JobPosting
    match_score: int = Field(ge=0, le=100)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    location_match: bool | None = None
    graduation_match: bool | None = None
    queue_track: QueueTrack = "steady"


class RecommendationBatch(BaseModel):
    generated_at: datetime
    engine: str
    profile_summary: str
    jobs: list[JobRecommendation]


class QueueAddRequest(BaseModel):
    job_ids: list[str] = Field(min_length=1, max_length=20)
    resume_id: str = ""


class ApplicationQueueItem(BaseModel):
    id: str
    job_id: str
    resume_id: str = ""
    status: QueueStatus = "planned"
    created_at: datetime
    updated_at: datetime
    recommendation: JobRecommendation

