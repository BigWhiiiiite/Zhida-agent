from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from .job_models import JobDiscoveryResult, JobPosting, OfficialJobSource
from .storage import get_job_source_run, save_discovered_jobs, save_job_source_run


MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_SOURCE_PAGES = 20
SOURCE_PAGE_DELAY_SECONDS = 0.12
BAIDU_CAMPUS_URL = "https://talent.baidu.com/jobs/list"
SOURCE_CONFIGS = {
    "baidu-campus": {
        "name": "百度校园招聘官网",
        "company": "百度",
        "official_url": BAIDU_CAMPUS_URL,
        "coverage": "官网校园招聘公开列表；最多同步 20 页并显示实际覆盖数量",
    },
}

SKILL_ALIASES = (
    ("Agent", ("agent", "智能体")),
    ("大语言模型", ("大语言模型", "大模型", "llm")),
    ("Python", ("python",)), ("Java", ("java",)), ("Go", ("golang", "go语言")),
    ("C++", ("c++",)), ("PyTorch", ("pytorch",)),
    ("PaddlePaddle", ("paddlepaddle",)), ("RAG", ("rag", "检索增强")),
    ("Linux", ("linux",)), ("数据结构", ("数据结构",)),
    ("分布式系统", ("分布式系统",)), ("自动化测试", ("自动化测试", "pytest")),
    ("后端开发", ("后端开发", "后端研发")), ("机器学习", ("机器学习",)),
    ("深度学习", ("深度学习",)), ("多模态", ("多模态",)), ("AIGC", ("aigc",)),
)
ROLE_TERMS = (
    "Agent", "智能体", "算法", "大模型", "后端", "全栈", "测试开发",
    "机器学习", "多模态", "产品", "研发", "软件工程",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _initial_data(content: str) -> dict:
    marker = re.search(r"window\.__INITIAL_DATA__\s*=\s*", content)
    if not marker:
        raise ValueError("官网页面没有公开的岗位初始数据")
    payload, _ = json.JSONDecoder().raw_decode(content[marker.end():])
    if not isinstance(payload, dict):
        raise ValueError("官网岗位初始数据格式异常")
    return payload


def _graduation_window(list_data: dict) -> str:
    configs = list_data.get("listConfig") or []
    graduate = next(
        (item for item in configs if item.get("recruitType") == "GRADUATE"), {}
    )
    text = " ".join(str(graduate.get(key, "")) for key in ("subtitle", "content"))
    dates = re.findall(r"(20\d{2})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})", text)
    if len(dates) >= 2:
        normalized = [f"{year}-{int(month):02d}-{int(day):02d}" for year, month, day in dates[:2]]
        return f"{normalized[0]} 至 {normalized[1]}"
    return str(graduate.get("subtitle") or "以官网当前校招说明为准")


def _locations(value: str) -> list[str]:
    locations = []
    for item in re.split(r"[,，/、]", value or ""):
        normalized = re.sub(r"市$", "", item.strip())
        if normalized and normalized not in locations:
            locations.append(normalized)
    return locations


def _education_requirement(value: str) -> str:
    for line in re.split(r"[\r\n]+", value or ""):
        line = re.sub(r"^[\s\-•·\d.、]+", "", line).strip()
        if line and re.search(r"本科|硕士|博士|学历", line):
            return line[:220]
    return "以职位详情为准"


def _terms(text: str, candidates: tuple[str, ...], limit: int) -> list[str]:
    lowered = text.casefold()
    result = []
    for term in candidates:
        if term.casefold() in lowered and term not in result:
            result.append(term)
    return result[:limit]


def _skill_terms(text: str, limit: int = 7) -> list[str]:
    lowered = text.casefold()
    return [canonical for canonical, aliases in SKILL_ALIASES
            if any(alias in lowered for alias in aliases)][:limit]


def _baidu_job(record: dict, graduation_window: str, discovered_at: datetime,
               scope: str) -> JobPosting | None:
    if record.get("isValid") is False:
        return None
    title = str(record.get("name") or "").strip()
    identifier = str(record.get("postId") or record.get("jobId") or "").strip()
    if not title or not identifier:
        return None
    code_match = re.search(r"\((J\d+)\)\s*$", title, re.I)
    job_code = code_match.group(1).upper() if code_match else ""
    clean_title = re.sub(r"\s*\(J\d+\)\s*$", "", title, flags=re.I)
    requirements = str(record.get("serviceCondition") or "").strip()
    work = str(record.get("workContent") or "").strip()
    corpus = f"{clean_title}\n{requirements}\n{work}"
    detail_url = f"https://talent.baidu.com/jobs/detail/GRADUATE/{identifier}"
    updated_at = str(record.get("updateDate") or record.get("publishDate") or "")
    evidence = ["百度官网校园招聘公开列表"]
    if job_code:
        evidence.append(f"官网职位编号 {job_code}")
    if updated_at:
        evidence.append(f"官网更新日期 {updated_at}")
    return JobPosting(
        id=f"baidu-{job_code.casefold() if job_code else identifier}",
        company="百度", title=clean_title, job_code=job_code,
        locations=_locations(str(record.get("workPlace") or "")),
        recruitment_type="校园招聘", graduation_window=graduation_window,
        education_requirement=_education_requirement(requirements),
        description=work[:1600], required_skills=_skill_terms(corpus),
        preferred_skills=[], role_keywords=_terms(corpus, ROLE_TERMS, 6),
        url=detail_url, source_name="百度校园招聘官网", source_url=detail_url,
        source_status="verify_on_open", apply_mode="direct",
        discovery_source="baidu-campus", discovered_at=discovered_at.isoformat(),
        source_updated_at=updated_at, discovery_scope=scope,
        discovery_evidence=evidence,
    )


def parse_baidu_campus_page(content: str, discovered_at: datetime | None = None
                             ) -> tuple[list[JobPosting], int | None, int]:
    timestamp = discovered_at or _now()
    payload = _initial_data(content)
    list_data = payload.get("listData") or {}
    records = list_data.get("listDetailData") or []
    if not isinstance(records, list):
        raise ValueError("官网岗位列表格式异常")
    total = list_data.get("total")
    total = int(total) if isinstance(total, (int, float, str)) and str(total).isdigit() else None
    page_num = int(list_data.get("pageNum") or 1)
    page_size = int(list_data.get("pageSize") or len(records) or 0)
    scope = f"官网公开列表第 {page_num} 页，扫描 {len(records)} 条"
    if total is not None:
        scope += f" / 共 {total} 条"
    window = _graduation_window(list_data)
    jobs = [job for record in records
            if (job := _baidu_job(record, window, timestamp, scope)) is not None]
    return jobs, total, max(0, len(records) - len(jobs))


def _check_official_response(response: httpx.Response, official_url: str) -> None:
    if response.status_code >= 400:
        raise ValueError(f"官网返回 HTTP {response.status_code}")
    expected_host = (urlparse(official_url).hostname or "").lower()
    if any((item.url.scheme != "https" or (item.url.host or "").lower() != expected_host)
           for item in [*response.history, response]):
        raise ValueError("官网请求发生了未授权的跨域重定向")
    if len(response.content) > MAX_SOURCE_BYTES:
        raise ValueError("官网页面超过安全读取上限")


async def _baidu_api_page(client: httpx.AsyncClient, page_number: int,
                           graduation_window: str, discovered_at: datetime
                           ) -> tuple[list[JobPosting], int | None, int, int]:
    response = await client.post(
        "/httservice/getPostListNew",
        headers={
            "Origin": "https://talent.baidu.com",
            "Referer": BAIDU_CAMPUS_URL,
            "X-Requested-With": "XMLHttpRequest",
        },
        data={
            "recruitType": "GRADUATE", "pageSize": "10", "keyWord": "",
            "curPage": str(page_number), "projectType": "",
        },
    )
    _check_official_response(response, BAIDU_CAMPUS_URL)
    payload = response.json()
    if payload.get("status") != "ok" or not isinstance(payload.get("data"), dict):
        raise ValueError(f"官网分页接口返回异常：{payload.get('message') or '未知错误'}")
    data = payload["data"]
    records = data.get("list") or []
    if not isinstance(records, list):
        raise ValueError("官网分页岗位列表格式异常")
    total_value = data.get("total")
    total = int(total_value) if str(total_value).isdigit() else None
    scope = f"官网公开列表第 {page_number} 页，扫描 {len(records)} 条"
    if total is not None:
        scope += f" / 共 {total} 条"
    jobs = [job for record in records
            if (job := _baidu_job(record, graduation_window, discovered_at, scope)) is not None]
    return jobs, total, len(records), max(0, len(records) - len(jobs))


def official_job_sources() -> list[OfficialJobSource]:
    result = []
    for source_id, config in SOURCE_CONFIGS.items():
        run = get_job_source_run(source_id) or {}
        result.append(OfficialJobSource(
            id=source_id, **config,
            last_status=run.get("status", "never"),
            last_completed_at=run.get("completed_at", ""),
            last_message=run.get("message", "尚未同步"),
            jobs_seen=run.get("jobs_seen", 0),
            total_available=run.get("total_available"),
            partial=run.get("partial", True),
        ))
    return result


def _save_run(result: JobDiscoveryResult) -> None:
    save_job_source_run({
        **result.model_dump(exclude={"jobs"}),
        "started_at": result.started_at.isoformat(),
        "completed_at": result.completed_at.isoformat(),
    })


async def sync_official_source(source_id: str,
                               transport: httpx.AsyncBaseTransport | None = None
                               ) -> JobDiscoveryResult:
    config = SOURCE_CONFIGS.get(source_id)
    if not config:
        raise LookupError("未知的官方岗位来源")
    started_at = _now()
    try:
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=True,
            base_url="https://talent.baidu.com",
            timeout=httpx.Timeout(25.0, connect=8.0),
            headers={"User-Agent": "Mozilla/5.0 (compatible; ZhidaJobDiscovery/0.1)"},
        ) as client:
            if source_id != "baidu-campus":
                raise ValueError("该来源暂未实现解析器")
            initial_error: Exception | None = None
            for attempt in range(2):
                response = await client.get(config["official_url"])
                _check_official_response(response, config["official_url"])
                try:
                    jobs, total, skipped = parse_baidu_campus_page(response.text, started_at)
                    initial = _initial_data(response.text).get("listData") or {}
                    break
                except (ValueError, json.JSONDecodeError) as exc:
                    initial_error = exc
                    if attempt == 0:
                        await asyncio.sleep(0.25)
            else:
                raise ValueError(f"官网初始数据连续两次解析失败：{initial_error}")
            graduation_window = _graduation_window(initial)
            records_scanned = len(initial.get("listDetailData") or [])
            page_size = int(initial.get("pageSize") or 10)
            total_pages = math.ceil(total / page_size) if total and page_size else 1
            page_failure = ""
            for page_number in range(2, min(total_pages, MAX_SOURCE_PAGES) + 1):
                try:
                    page_jobs, page_total, scanned, page_skipped = await _baidu_api_page(
                        client, page_number, graduation_window, started_at
                    )
                except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                    page_failure = f"第 {page_number} 页失败：{exc}"
                    break
                jobs.extend(page_jobs)
                records_scanned += scanned
                skipped += page_skipped
                total = page_total or total
                await asyncio.sleep(SOURCE_PAGE_DELAY_SECONDS)
        if not jobs:
            raise ValueError("官网页面没有解析出可用的校园招聘岗位")
        jobs = list({job.id: job for job in jobs}.values())
        completed_at = _now()
        created, updated = save_discovered_jobs(
            source_id, [job.model_dump(mode="json") for job in jobs], completed_at.isoformat()
        )
        partial = bool(page_failure) or total is None or records_scanned < total
        status = "partial" if partial else "success"
        message = f"从官网同步 {len(jobs)} 个岗位"
        if page_failure:
            message += f"（已保留成功结果；{page_failure}）"
        elif total_pages > MAX_SOURCE_PAGES:
            message += f"（达到 {MAX_SOURCE_PAGES} 页安全上限，共 {total} 个）"
        elif partial and total is not None:
            message += f"（当前扫描 {records_scanned}/{total} 个）"
        result = JobDiscoveryResult(
            source_id=source_id, source_name=config["name"], status=status,
            started_at=started_at, completed_at=completed_at, jobs_seen=len(jobs),
            created=created, updated=updated, skipped=skipped, total_available=total,
            partial=partial, message=message, jobs=jobs,
        )
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        result = JobDiscoveryResult(
            source_id=source_id, source_name=config["name"], status="failed",
            started_at=started_at, completed_at=_now(), partial=True,
            message=f"官网同步失败：{exc}",
        )
    _save_run(result)
    return result
