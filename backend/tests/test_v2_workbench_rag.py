"""Synthetic bridge tests: index results, never old per-record vector scans."""
from datetime import datetime, timezone

import pytest

from app import job_rag, storage
from app.backend_v2 import workbench_rag
from app.job_models import JobPosting, JobRecommendation, RecommendationBatch
from app.models import CandidateProfile


def recommendation(identifier, required=(), preferred=()):
    return JobRecommendation(job=JobPosting(id=identifier, company="合成公司", title="合成开发岗位",
        required_skills=list(required), preferred_skills=list(preferred),
        url="https://jobs.example.invalid/test", source_name="合成源", source_url="https://jobs.example.invalid"),
        match_score=75, matched_skills=[*required, *preferred], formal_queue_eligible=True)


def batch(items):
    return RecommendationBatch(generated_at=datetime.now(timezone.utc), engine="synthetic",
                               profile_summary="synthetic", jobs=items)


def evidence(identifier, text, kind="project"):
    return {"chunk_id": identifier, "entity_type": kind, "parent_context": "合成项目",
            "source_path": "projects[0].description", "text": text,
            "score": 1000, "auto_fill_allowed": False, "trust": "unverified_source"}


@pytest.fixture
def indexed_only(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("V2 bridge must not scan old records, vectors or SQLite")
    for name in ("_indexed_chunks", "_embed", "_dot", "rag_fts_ranks"):
        monkeypatch.setattr(job_rag, name, forbidden)
    monkeypatch.setattr(storage, "_connection", forbidden)
    token = storage.set_current_user("synthetic-owner")
    yield
    storage.reset_current_user(token)


def test_index_scope_dedup_and_direct_evidence_preferred(monkeypatch, indexed_only):
    calls = []

    class Search:
        def search(self, user, query, **filters):
            calls.append((user, query, filters))
            if query == "Python":
                return {"mode": "ann", "evidence": [evidence("wrong-kind", "Python", "education"),
                    evidence("related", "完成检索服务的工程设计"), evidence("direct", "使用 Python 编写检索服务")]}
            return {"mode": "ann", "evidence": [evidence("weak", "完成工程设计，与缓存系统可能相关")]}

    monkeypatch.setattr(workbench_rag, "service", lambda: Search())
    result = workbench_rag.enrich(batch([recommendation("one", ("Python", "Redis")),
        recommendation("two", (), ("Python",))]), CandidateProfile(), {})
    assert len(calls) == 2  # Same requirement shared across jobs/kinds is one search.
    assert all(user == "synthetic-owner" and filters == {
        "source_ids": ["workbench:profile:synthetic-owner"], "limit": 5} for user, _, filters in calls)
    item = next(item for item in result.jobs if item.job.id == "one")
    python = next(item for item in item.evidence_matches if item.requirement == "Python")
    assert python.support == "direct" and python.evidence_id == "direct"
    assert python.semantic_score == 0  # RRF=1000 is not cosine or confidence.
    assert "Python" in item.matched_skills
    assert "Redis" in item.missing_skills and "Redis" not in item.matched_skills
    redis = next(item for item in item.evidence_matches if item.requirement == "Redis")
    assert redis.support == "related"
    assert all(match.evidence_id != "wrong-kind" for match in item.evidence_matches)
    assert result.engine == "evidence-rag-postgres-v2"
    assert result.rag_status == "ready"


def test_unretrieved_tail_cannot_keep_old_skill_matches(monkeypatch, indexed_only):
    monkeypatch.setattr(job_rag, "MAX_ENRICHED_JOBS", 1)

    class Empty:
        def search(self, *args, **kwargs):
            return {"mode": "not_indexed", "evidence": []}

    monkeypatch.setattr(workbench_rag, "service", lambda: Empty())
    result = workbench_rag.enrich(batch([recommendation("top", ("Python",)),
        recommendation("tail", ("Rust",))]), CandidateProfile(), {})
    assert result.rag_status == "no_evidence"
    for item in result.jobs:
        assert item.matched_skills == []
        assert item.missing_skills == item.job.required_skills
        assert item.formal_queue_eligible is True  # Eligibility is not a retrieval claim.


def test_react_frontend_not_direct_agent_react_skill(monkeypatch, indexed_only):
    class Search:
        def search(self, *args, **kwargs):
            return {"mode": "hybrid_degraded", "evidence": [evidence("ui", "用 React 开发前端页面")]}

    monkeypatch.setattr(workbench_rag, "service", lambda: Search())
    result = workbench_rag.enrich(batch([recommendation("one", ("ReAct",))]), CandidateProfile(), {})
    assert result.jobs[0].matched_skills == []
    assert result.jobs[0].evidence_matches[0].support == "related"
    assert result.rag_status == "keyword_only"
