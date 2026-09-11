from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from .job_models import JobPosting, JobVerification, LiveJobStatus
from .storage import save_job_verification


MAX_RESPONSE_BYTES = 3 * 1024 * 1024
CLOSED_HINT = re.compile(
    r"该职位(?:已)?(?:下线|关闭|停止招聘)|职位不存在|职位已失效|"
    r"position (?:has been |is )?closed|job (?:is )?no longer available", re.I,
)
APPLY_HINT = re.compile(
    r"申请职位|立即投递|投递简历|申请该职位|apply now|apply for (?:this|the) (?:job|position)", re.I,
)
CAMPUS_HINT = re.compile(r"校园招聘|校招|应届|graduate|campus|2027届", re.I)
DYNAMIC_HOSTS = {"join.qq.com"}


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9一-鿿]", "", value.casefold())


def _page_text(content: str) -> str:
    without_markup = re.sub(r"<[^>]+>", " ", content)
    return re.sub(r"\s+", " ", html.unescape(without_markup)).strip()


def _page_title(content: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", content, re.I | re.S)
    return re.sub(r"\s+", " ", html.unescape(match.group(1))).strip()[:300] if match else ""


def _expected_hosts(job: JobPosting) -> set[str]:
    return {
        host for value in (job.url, job.source_url)
        if (host := (urlparse(value).hostname or "").lower())
    }


def _result(job: JobPosting, status: LiveJobStatus, checked_at: datetime, *, final_url: str = "",
            http_status: int | None = None, page_title: str = "", evidence: list[str] | None = None,
            message: str) -> JobVerification:
    verification = JobVerification(
        job_id=job.id, status=status, checked_at=checked_at, official_url=job.source_url,
        final_url=final_url, http_status=http_status, page_title=page_title,
        evidence=evidence or [], message=message, can_proceed=status == "open",
    )
    save_job_verification({**verification.model_dump(mode="json"),
                           "checked_at": verification.checked_at.isoformat()})
    return verification


async def verify_official_job(job: JobPosting,
                              transport: httpx.AsyncBaseTransport | None = None) -> JobVerification:
    """Verify one catalog entry without accepting arbitrary URLs or claiming dynamic pages are open."""
    checked_at = datetime.now(timezone.utc)
    expected_hosts = _expected_hosts(job)
    source = urlparse(job.source_url)
    if source.scheme != "https" or not source.hostname or source.hostname.lower() not in expected_hosts:
        return _result(job, "mismatch", checked_at, message="岗位来源不是可验证的 HTTPS 官网")

    try:
        async with httpx.AsyncClient(
            transport=transport, follow_redirects=True, timeout=httpx.Timeout(15.0, connect=6.0),
            headers={"User-Agent": "Mozilla/5.0 (compatible; ZhidaJobVerifier/0.1)"},
        ) as client:
            response = await client.get(job.source_url)
    except (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError) as exc:
        return _result(job, "unreachable", checked_at, message=f"官网请求失败：{type(exc).__name__}")

    final_url = str(response.url)
    visited = [*response.history, response]
    if any((item.url.scheme != "https" or (item.url.host or "").lower() not in expected_hosts)
           for item in visited):
        return _result(job, "mismatch", checked_at, final_url=final_url,
                       http_status=response.status_code, message="官网请求被重定向到未授权域名")
    if response.status_code in {404, 410}:
        return _result(job, "closed", checked_at, final_url=final_url,
                       http_status=response.status_code, evidence=[f"HTTP {response.status_code}"],
                       message="官方页面已不存在")
    if response.status_code >= 400:
        return _result(job, "unreachable", checked_at, final_url=final_url,
                       http_status=response.status_code, evidence=[f"HTTP {response.status_code}"],
                       message="官网暂时无法完成核验")
    if len(response.content) > MAX_RESPONSE_BYTES:
        return _result(job, "manual_gate", checked_at, final_url=final_url,
                       http_status=response.status_code, evidence=[f"HTTP {response.status_code}"],
                       message="官方页面过大，需在浏览器中二次核验")

    content = response.text
    title = _page_title(content)
    text = _page_text(content)[:1_000_000]
    if CLOSED_HINT.search(text) or CLOSED_HINT.search(title):
        return _result(job, "closed", checked_at, final_url=final_url,
                       http_status=response.status_code, page_title=title,
                       evidence=[f"HTTP {response.status_code}", "页面包含职位关闭提示"],
                       message="官方页面显示该职位已不可投")

    compact_text = _compact(text)
    code = _compact(job.job_code)
    meaningful_code = bool(code and code not in {"projectentry", "entry"})
    title_key = _compact(job.title)
    identity = (meaningful_code and code in compact_text) or (
        len(title_key) >= 6 and title_key[: min(18, len(title_key))] in compact_text
    )
    if job.apply_mode == "search":
        identity = identity or bool(CAMPUS_HINT.search(text))
    apply_match = APPLY_HINT.search(text)
    evidence = [f"HTTP {response.status_code}"]
    if meaningful_code and code in compact_text:
        evidence.append(f"匹配职位编号 {job.job_code}")
    elif identity:
        evidence.append("匹配官方页职位/招聘项目标识")
    if apply_match:
        evidence.append(f"识别到申请入口“{apply_match.group(0)}”")

    if identity and apply_match:
        return _result(job, "open", checked_at, final_url=final_url,
                       http_status=response.status_code, page_title=title, evidence=evidence,
                       message="官方页的职位标识与申请入口均可用")
    host = (urlparse(final_url).hostname or "").lower()
    if job.apply_mode == "direct" and not identity and host not in DYNAMIC_HOSTS:
        return _result(job, "mismatch", checked_at, final_url=final_url,
                       http_status=response.status_code, page_title=title, evidence=evidence,
                       message="页面可访问，但未匹配到期望的职位编号或标题")
    return _result(job, "manual_gate", checked_at, final_url=final_url,
                   http_status=response.status_code, page_title=title, evidence=evidence,
                   message="静态核验证据不足，需打开官网由浏览器二次核验")
