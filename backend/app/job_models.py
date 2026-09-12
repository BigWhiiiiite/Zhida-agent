from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


SourceStatus = Literal["verified", "verify_on_open"]
LiveJobStatus = Literal["not_checked", "open", "closed", "manual_gate", "mismatch", "unreachable"]
ApplyMode = Literal["direct", "search"]
QueueTrack = Literal["steady", "stretch"]
QueueStatus = Literal[
    "planned", "in_progress", "needs_review", "ready_to_submit",
    "submitted", "interview", "offer", "rejected", "withdrawn",
]
DiscoverySyncStatus = Literal["never", "success", "partial", "failed"]


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
    live_status: LiveJobStatus = "not_checked"
    last_checked_at: str = ""
    verification_message: str = ""
    verification_evidence: list[str] = Field(default_factory=list)
    discovery_source: str = ""
    discovered_at: str = ""
    source_updated_at: str = ""
    discovery_scope: str = ""
    discovery_evidence: list[str] = Field(default_factory=list)


class JobRecommendation(BaseModel):
    job: JobPosting
    match_score: int = Field(ge=0, le=100)
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    location_match: bool | None = None
    graduation_match: bool | None = None
    queue_track: QueueTrack = "steady"
    formal_queue_eligible: bool = False
    gate_reasons: list[str] = Field(default_factory=list)


class RecommendationBatch(BaseModel):
    generated_at: datetime
    engine: str
    profile_summary: str
    available_locations: list[str] = Field(default_factory=list)
    selected_location: str = ""
    jobs: list[JobRecommendation]


class QueueAddRequest(BaseModel):
    job_ids: list[str] = Field(min_length=1, max_length=20)
    resume_id: str = ""


class ApplicationQueueItem(BaseModel):
    id: str
    job_id: str
    resume_id: str = ""
    status: QueueStatus = "planned"
    notes: str = ""
    application_id: str = ""
    status_changed_at: datetime
    submitted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    recommendation: JobRecommendation


class ApplicationQueueUpdate(BaseModel):
    status: QueueStatus | None = None
    notes: str | None = Field(default=None, max_length=2000)
    application_id: str | None = Field(default=None, max_length=200)
    candidate_confirmed: bool = False


class ApplicationReadiness(BaseModel):
    ready: bool
    score: int = Field(ge=0, le=100)
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    resume_count: int = 0
    default_resume_id: str = ""
    pending_resume_fields: int = 0
    pending_conflicts: int = 0
    queued_jobs: int = 0
    official_sources_ready: int = 0
    official_sources_total: int = 0


class JobVerification(BaseModel):
    job_id: str
    status: LiveJobStatus
    checked_at: datetime
    official_url: str
    final_url: str = ""
    http_status: int | None = None
    page_title: str = ""
    evidence: list[str] = Field(default_factory=list)
    message: str
    can_proceed: bool = False


class OfficialJobSource(BaseModel):
    id: str
    name: str
    company: str
    official_url: str
    enabled: bool = True
    coverage: str
    last_status: DiscoverySyncStatus = "never"
    last_completed_at: str = ""
    last_message: str = "尚未同步"
    jobs_seen: int = 0
    total_available: int | None = None
    partial: bool = True


class JobDiscoveryResult(BaseModel):
    source_id: str
    source_name: str
    status: DiscoverySyncStatus
    started_at: datetime
    completed_at: datetime
    jobs_seen: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    total_available: int | None = None
    partial: bool = True
    message: str
    jobs: list[JobPosting] = Field(default_factory=list)
