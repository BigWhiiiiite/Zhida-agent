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
DiscoveryAdapter = Literal["auto", "baidu", "greenhouse", "lever", "ashby", "smartrecruiters", "jsonld"]
CompanySize = Literal["large", "growth", "startup", "unknown"]


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
    company_size: CompanySize = "unknown"


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
    evidence_matches: list["JobEvidenceMatch"] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)


class JobEvidenceMatch(BaseModel):
    requirement: str
    requirement_type: Literal["required", "preferred"]
    evidence_id: str
    source_kind: Literal["project", "internship"]
    source_title: str
    source_path: str
    quote: str
    support: Literal["direct", "related"]
    lexical_score: float = Field(ge=0, le=1)
    semantic_score: float = Field(ge=-1, le=1)


class JobEvidenceExplanation(BaseModel):
    job_id: str
    status: Literal["model", "local_fallback", "no_evidence"]
    model: str = ""
    summary: str
    supported_reasons: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class RecommendationBatch(BaseModel):
    generated_at: datetime
    engine: str
    profile_summary: str
    available_locations: list[str] = Field(default_factory=list)
    selected_location: str = ""
    jobs: list[JobRecommendation]
    rag_status: Literal["not_run", "ready", "keyword_only", "no_evidence"] = "not_run"
    rag_model: str = ""
    rag_evidence_count: int = 0
    rag_enriched_jobs: int = 0
    rag_message: str = ""


class RagRecommendationRequest(BaseModel):
    location: str = Field(default="", max_length=120)
    query: str = Field(default="", max_length=300)
    company_sizes: list[CompanySize] = Field(default_factory=list, max_length=4)


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
    assistance_started_at: datetime | None = None
    confirmation_pending: bool = False
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
    adapter: DiscoveryAdapter = "auto"
    source_key: str = ""
    company_size: CompanySize = "unknown"
    user_added: bool = False
    enabled: bool = True
    coverage: str
    last_status: DiscoverySyncStatus = "never"
    last_completed_at: str = ""
    last_message: str = "尚未同步"
    jobs_seen: int = 0
    total_available: int | None = None
    partial: bool = True


class JobSourceCreate(BaseModel):
    company: str = Field(min_length=1, max_length=120)
    official_url: str = Field(min_length=8, max_length=1000)
    adapter: DiscoveryAdapter = "auto"
    source_key: str = Field(default="", max_length=200)
    company_size: CompanySize = "unknown"


class SmartJobSearchRequest(BaseModel):
    query: str = Field(default="", max_length=300)
    location: str = Field(default="", max_length=120)
    company_sizes: list[CompanySize] = Field(default_factory=list, max_length=4)
    sync_sources: bool = True
    max_sources: int = Field(default=8, ge=1, le=20)


class JobSearchSourceSummary(BaseModel):
    source_id: str
    source_name: str
    status: DiscoverySyncStatus
    jobs_seen: int = 0
    message: str = ""


class SmartJobSearchResult(BaseModel):
    query: str
    generated_at: datetime
    synced_sources: int = 0
    successful_sources: int = 0
    failed_sources: int = 0
    discovered_jobs: int = 0
    sources: list[JobSearchSourceSummary] = Field(default_factory=list)
    batch: RecommendationBatch


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
