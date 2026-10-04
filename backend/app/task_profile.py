"""Version-bound application context. Structured facts remain authoritative."""
from __future__ import annotations

import hashlib
import json

from .models import CandidateProfile, ResumeRecord

# Identity/contact data belongs to the person. These fields belong to the
# deliberately selected CV. Empty version fields must NOT borrow another CV.
VERSION_FIELDS = (
    "education", "internships", "projects", "skills", "languages", "certificates",
    "awards", "summary", "target_role",
)


def compose_task_profile(master: CandidateProfile, resume: ResumeRecord | None,
                         company_scope: str = "") -> CandidateProfile:
    profile = master.model_copy(deep=True)
    # Old unscoped free-form answers lack a tenant/version identity. Keep them
    # in storage for review, but do not use them as automatic filling evidence.
    profile.application_answers = {}
    resume_id = resume.id if resume else ""
    if resume:
        if resume.status in {"pending", "parsing", "failed"}:
            raise ValueError("所选简历尚未解析完成，请先完成解析及资料检查")
        version = resume.profile.model_copy(deep=True)
        for field in VERSION_FIELDS:
            evidence = [item for item in resume.evidence if item.field_path == field]
            value = getattr(version, field)
            if evidence and any(item.status not in {"confirmed", "edited"} for item in evidence):
                value = [] if isinstance(value, list) else ""
            setattr(profile, field, value)
        # Legacy unscoped answers cannot safely override version-specific facts.
        profile.application_answers = {}
    profile.application_answer_memory = [
        item for item in master.application_answer_memory
        if item.resume_id == resume_id and item.company_scope == company_scope
    ]
    return profile


def context_token(profile: CandidateProfile, resume: ResumeRecord | None) -> str:
    payload = {"profile": profile.model_dump(mode="json"),
               "resume_id": resume.id if resume else "",
               "resume_updated_at": str(resume.updated_at) if resume else ""}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
