"""Offline checks for the evidence RAG. Run: .venv/bin/python job_rag_smoke_test.py"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import job_rag, storage
from app.job_models import JobPosting, JobRecommendation
from app.models import CandidateProfile, Experience, Project, ResumeProfile
from fastapi.testclient import TestClient


def fake_embed(texts: list[str], _model: str) -> list[tuple[float, ...]]:
    # Deterministic dense vectors for CI; one integration check below uses the
    # actual locally cached model separately.
    return [(1.0, 0.0) if "Agent" in text or "智能体" in text else (0.0, 1.0)
            for text in texts]


def main() -> None:
    with TemporaryDirectory() as temporary:
        storage.DB_PATH = Path(temporary) / "rag.db"
        storage.initialize()
        token = storage.set_current_user("rag-test-user-a")
        try:
            profile = CandidateProfile(
                target_role="Agent 工程师",
                skills=["LangChain"],  # A profile keyword alone is not evidence.
                projects=[Project(
                    name="招聘助手", role="后端研发",
                    description="使用 Python 实现 Agent 工具调用和任务规划。负责岗位推荐和日志采集。",
                    achievements=["编写 Pytest 测试并自动化评测流程。"],
                    technologies=["FastAPI"],
                )],
                internships=[Experience(
                    organization="测试公司", role="前端实习生",
                    description="维护 React 前端组件，优化职位筛选界面。",
                )],
            )
            storage.get_profile()
            storage.save_profile(ResumeProfile(**profile.model_dump(exclude={"id", "created_at", "updated_at"})))
            chunks = job_rag.split_profile_evidence(profile)
            assert len(chunks) >= 5, "A whole project must not be one chunk"
            assert all(card.source_path and card.quote for card in chunks)
            assert not any("LangChain" in card.quote for card in chunks)

            job = JobPosting(
                id="synthetic-agent-role", company="测试公司", title="Agent 工程师",
                required_skills=["Agent", "Python", "LangChain", "RAG"],
                preferred_skills=["React", "FastAPI"],
                url="https://jobs.example.test/agent", source_name="测试官网",
                source_url="https://jobs.example.test/agent",
            )
            item = JobRecommendation(job=job, match_score=80, matched_skills=["Agent", "Python", "LangChain", "React"], missing_skills=["RAG"])
            from datetime import datetime, timezone
            from app.job_models import RecommendationBatch
            batch = RecommendationBatch(generated_at=datetime.now(timezone.utc), engine="test", profile_summary="test", jobs=[item])
            with patch.object(job_rag, "_embed", fake_embed):
                result = job_rag.enrich_recommendation_batch(batch, profile, {})
            assert result.rag_status == "ready" and result.rag_evidence_count == len(chunks)
            enriched = result.jobs[0]
            assert {"Agent", "Python", "React", "FastAPI"}.issubset(set(enriched.matched_skills))
            assert "LangChain" not in enriched.matched_skills
            assert {"LangChain", "RAG"}.issubset(set(enriched.evidence_gaps))
            assert enriched.match_score < item.match_score
            assert all(match.quote in next(card.quote for card in chunks if card.chunk_id == match.evidence_id)
                       for match in enriched.evidence_matches)
            with patch("agents.Runner.run", side_effect=RuntimeError("proxy unavailable")):
                explanation = asyncio.run(job_rag.explain_evidence_matches(enriched))
            assert explanation.status == "local_fallback"
            assert explanation.evidence_ids and explanation.supported_reasons
            with patch.object(job_rag, "_embed", side_effect=RuntimeError("model unavailable")):
                lexical_fallback = job_rag.enrich_recommendation_batch(batch, profile, {})
            assert lexical_fallback.rag_status == "keyword_only"
            assert "Python" in lexical_fallback.jobs[0].matched_skills

            profile_hash = job_rag._profile_hash(chunks)
            assert storage.get_rag_index(profile_hash, job_rag.MODEL_NAME)
            assert storage.rag_fts_ranks("Python")

            other = storage.set_current_user("rag-test-user-b")
            try:
                assert storage.get_rag_index(profile_hash, job_rag.MODEL_NAME) is None
                assert storage.rag_fts_ranks("Python") == {}
            finally:
                storage.reset_current_user(other)

            # Semantic proximity may be shown as a lead, never as a proven skill.
            related = job_rag._best_evidence("数据库调优", "required", [
                job_rag.EvidenceChunk("x", "project", "测试项目", "projects[0].description[0]", "实现智能体工具编排服务", "测试", 1.0, (1.0, 0.0))
            ], (1.0, 0.0), {})
            assert related is not None and related.support == "related"
            ui_card = job_rag.EvidenceChunk(
                "ui", "internship", "前端实习", "internships[0].description[0]",
                "维护 React 前端组件", "测试", 1.0,
            )
            assert job_rag._lexical_score("React", ui_card, {}, None) == 1.0
            assert job_rag._lexical_score("ReAct", ui_card, {"ReAct": ("react agent",)}, 1) == 0.0
            no_evidence = job_rag.enrich_recommendation_batch(
                batch, CandidateProfile(skills=["Agent", "Python"]), {},
            )
            assert no_evidence.rag_status == "no_evidence"
            assert no_evidence.jobs[0].matched_skills == []

            from app import main as api_main
            local_token = storage.set_current_user(storage.LOCAL_USER_ID)
            try:
                storage.get_profile()
                storage.save_profile(ResumeProfile(**profile.model_dump(exclude={"id", "created_at", "updated_at"})))
            finally:
                storage.reset_current_user(local_token)
            old_auth = os.environ.get("APP_AUTH_REQUIRED")
            os.environ["APP_AUTH_REQUIRED"] = "false"
            try:
                with patch.object(job_rag, "_embed", fake_embed), TestClient(api_main.app) as client:
                    response = client.post("/api/jobs/recommendations/rag", json={
                        "location": "", "query": "", "company_sizes": [],
                    })
                    assert response.status_code == 200, response.text
                    payload = response.json()
                    assert payload["rag_status"] == "ready" and payload["rag_evidence_count"] == len(chunks), (payload["rag_status"], payload["rag_evidence_count"])
                    assert any(item["evidence_matches"] for item in payload["jobs"])
            finally:
                if old_auth is None:
                    os.environ.pop("APP_AUTH_REQUIRED", None)
                else:
                    os.environ["APP_AUTH_REQUIRED"] = old_auth

            storage.save_profile(ResumeProfile(**profile.model_dump(exclude={"id", "created_at", "updated_at"})))
            assert storage.get_rag_index(profile_hash, job_rag.MODEL_NAME) is None
            assert storage.rag_fts_ranks("Python") == {}
        finally:
            storage.reset_current_user(token)
    print("job_rag_smoke_test: OK")


if __name__ == "__main__":
    main()
