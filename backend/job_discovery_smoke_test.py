"""Offline smoke tests for official job discovery and catalog merging."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from app import storage
from app.job_discovery import official_job_sources, sync_official_source
from app.job_recommendations import catalog_job, recommendation_batch
from app.job_verification import verify_official_job
from app.models import CandidateProfile, Education


def official_page() -> str:
    primary = {
        "name": "北京-Agent平台研发工程师(J12345)",
        "jobId": "11111111-2222-3333-4444-555555555555",
        "postId": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
        "workPlace": "北京市,上海市",
        "serviceCondition": "-本科及以上学历，计算机相关专业\n-熟练使用Python和Linux",
        "workContent": "负责大模型Agent平台、RAG和后端开发",
        "publishDate": "2026-07-08",
        "updateDate": "2026-09-10",
        "isValid": True,
    }
    fillers = [{
        "name": f"北京-软件研发工程师(J2000{index})",
        "postId": f"00000000-0000-0000-0000-00000000000{index}",
        "workPlace": "北京市", "serviceCondition": "本科及以上学历",
        "workContent": "负责软件工程研发", "isValid": True,
    } for index in range(1, 10)]
    payload = {
        "listData": {
            "pageNum": 1,
            "pageSize": 10,
            "total": 11,
            "listConfig": [{
                "recruitType": "GRADUATE",
                "subtitle": "面向全球2027届毕业生，毕业时间在2026年9月1日到2027年8月31日之间",
            }],
            "listDetailData": [primary, *fillers],
        }
    }
    return f"<html><script>window.__INITIAL_DATA__ ={json.dumps(payload)}; window.prefix='/jobs'</script></html>"


async def main() -> None:
    with TemporaryDirectory() as temporary:
        storage.DB_PATH = Path(temporary) / "discovery.db"
        storage.initialize()

        def source_page(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, request=request, text=official_page())
            return httpx.Response(200, request=request, json={
                "status": "ok",
                "data": {
                    "pageNum": 2, "pageSize": 10, "total": "11",
                    "list": [{
                        "name": "深圳-大模型应用工程师(J99999)",
                        "postId": "99999999-8888-7777-6666-555555555555",
                        "workPlace": "深圳市", "serviceCondition": "本科及以上学历",
                        "workContent": "负责Agent和大模型应用研发", "isValid": True,
                    }],
                },
            })

        result = await sync_official_source(
            "baidu-campus", httpx.MockTransport(source_page)
        )
        assert result.status == "success"
        assert result.jobs_seen == 11 and result.skipped == 0
        assert result.created == 11 and result.total_available == 11

        source = official_job_sources()[0]
        assert source.last_status == "success" and source.jobs_seen == 11
        assert source.total_available == 11 and not source.partial

        job = catalog_job("baidu-j12345")
        assert job is not None
        assert job.locations == ["北京", "上海"]
        assert job.graduation_window == "2026-09-01 至 2027-08-31"
        assert job.education_requirement.startswith("本科及以上学历")
        assert job.discovery_source == "baidu-campus"
        assert "Agent" in job.required_skills and "Python" in job.required_skills

        profile = CandidateProfile(
            target_role="Agent 开发工程师", target_cities=["北京"],
            skills=["Agent", "Python", "RAG", "后端开发"],
            education=[Education(school="Test University", end_date="2026-12")],
        )
        before = next(item for item in recommendation_batch(profile, "北京").jobs
                      if item.job.id == job.id)
        assert before.graduation_match is True and before.location_match is True
        assert not before.formal_queue_eligible

        opened = await verify_official_job(job, httpx.MockTransport(
            lambda request: httpx.Response(
                200, request=request,
                text="<title>北京-Agent平台研发工程师(J12345)</title><button>申请职位</button>",
            )
        ))
        assert opened.status == "open"
        after = next(item for item in recommendation_batch(profile, "北京").jobs
                     if item.job.id == job.id)
        assert after.formal_queue_eligible

        repeated = await sync_official_source(
            "baidu-campus", httpx.MockTransport(source_page)
        )
        assert repeated.created == 0 and repeated.updated == 11

        def broken_second_page(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, request=request, text=official_page())
            return httpx.Response(503, request=request, text="temporarily unavailable")

        partial = await sync_official_source(
            "baidu-campus", httpx.MockTransport(broken_second_page)
        )
        assert partial.status == "partial" and partial.jobs_seen == 10
        assert "第 2 页失败" in partial.message

        def hostile_redirect(request: httpx.Request) -> httpx.Response:
            if request.url.host == "talent.baidu.com":
                return httpx.Response(302, request=request,
                                      headers={"location": "https://evil.example/jobs"})
            return httpx.Response(200, request=request, text=official_page())

        blocked = await sync_official_source(
            "baidu-campus", httpx.MockTransport(hostile_redirect)
        )
        assert blocked.status == "failed"
        assert "跨域重定向" in blocked.message

        try:
            await sync_official_source("unknown-source")
        except LookupError:
            pass
        else:
            raise AssertionError("unknown source must fail closed")

    print("Official job discovery smoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
