"""Offline smoke tests for official-source job verification."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from app import storage
from app.job_recommendations import catalog_job, recommendation_batch
from app.job_verification import verify_official_job
from app.models import CandidateProfile, Education


async def main() -> None:
    with TemporaryDirectory() as temporary:
        storage.DB_PATH = Path(temporary) / "verification.db"
        storage.initialize()
        job = catalog_job("baidu-j99974-agent-fullstack")
        assert job is not None

        def open_page(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, request=request, text="""
            <html><head><title>2027AIDU-Agent应用全栈工程师(J99974)</title></head>
            <body><h1>2027AIDU-Agent应用全栈工程师(J99974)</h1>
            <p>北京市 2027届 校园招聘</p><button>申请职位</button></body></html>
            """)

        opened = await verify_official_job(job, httpx.MockTransport(open_page))
        assert opened.status == "open" and opened.can_proceed
        assert any("J99974" in item for item in opened.evidence)

        profile = CandidateProfile(
            target_role="Agent 应用工程师", target_cities=["北京"],
            skills=["Python", "FastAPI", "React", "Agent", "RAG"],
            education=[Education(school="Test University", end_date="2026-12")],
        )
        recommendation = next(
            item for item in recommendation_batch(profile).jobs if item.job.id == job.id
        )
        assert recommendation.job.live_status == "open"
        assert recommendation.formal_queue_eligible

        storage.save_job_verification({
            **opened.model_dump(mode="json"),
            "checked_at": (datetime.now(timezone.utc) - timedelta(hours=7)).isoformat(),
        })
        stale = next(item for item in recommendation_batch(profile).jobs if item.job.id == job.id)
        assert stale.job.live_status == "not_checked"
        assert "超过 6 小时" in stale.job.verification_message
        assert not stale.formal_queue_eligible

        closed_job = job.model_copy(update={"id": "closed-job"})
        closed = await verify_official_job(closed_job, httpx.MockTransport(
            lambda request: httpx.Response(200, request=request,
                                           text="<title>职位已关闭</title><p>该职位已下线</p>")
        ))
        assert closed.status == "closed" and not closed.can_proceed

        mismatch_job = job.model_copy(update={"id": "mismatch-job"})
        mismatch = await verify_official_job(mismatch_job, httpx.MockTransport(
            lambda request: httpx.Response(200, request=request,
                                           text="<title>其他职位</title><button>申请职位</button>")
        ))
        assert mismatch.status == "mismatch"

        def hostile_redirect(request: httpx.Request) -> httpx.Response:
            if request.url.host == "talent.baidu.com":
                return httpx.Response(302, request=request, headers={"location": "https://evil.example/job"})
            return httpx.Response(200, request=request, text="J99974 申请职位")

        redirect_job = job.model_copy(update={"id": "redirect-job"})
        redirected = await verify_official_job(redirect_job, httpx.MockTransport(hostile_redirect))
        assert redirected.status == "mismatch"

        def network_error(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        unreachable_job = job.model_copy(update={"id": "unreachable-job"})
        unreachable = await verify_official_job(unreachable_job, httpx.MockTransport(network_error))
        assert unreachable.status == "unreachable" and not unreachable.can_proceed

    print("Official job verification smoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
