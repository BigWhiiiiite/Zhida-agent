from __future__ import annotations

import asyncio
import hashlib
import html
import ipaddress
import json
import math
import re
import socket
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from .job_models import (DiscoveryAdapter, JobDiscoveryResult, JobPosting,
                         JobSourceCreate, OfficialJobSource)
from .storage import (current_user_id, delete_custom_job_source,
                      get_job_source_run, list_custom_job_sources,
                      save_custom_job_source, save_discovered_jobs,
                      save_job_source_run)


MAX_SOURCE_BYTES = 25 * 1024 * 1024
MAX_SOURCE_PAGES = 20
SOURCE_PAGE_DELAY_SECONDS = 0.12
BAIDU_CAMPUS_URL = "https://talent.baidu.com/jobs/list"

# These are source connectors, not a hard-coded job catalog. Each sync reads the
# current official ATS feed. Users can add more official sources from the UI.
SOURCE_CONFIGS: dict[str, dict[str, Any]] = {
    "baidu-campus": {
        "name": "百度校园招聘官网", "company": "百度",
        "official_url": BAIDU_CAMPUS_URL, "adapter": "baidu", "source_key": "",
        "company_size": "large", "user_added": False, "enabled": True,
        "coverage": "官网校园招聘公开列表；最多同步 20 页并显示实际覆盖数量",
    },
    "scale-greenhouse": {
        "name": "Scale AI 官方职位", "company": "Scale AI",
        "official_url": "https://boards.greenhouse.io/scaleai",
        "adapter": "greenhouse", "source_key": "scaleai", "company_size": "growth",
        "user_added": False, "enabled": True,
        "coverage": "Greenhouse 官方公开 Job Board API；包含当前公开职位",
    },
    "perplexity-ashby": {
        "name": "Perplexity 官方职位", "company": "Perplexity",
        "official_url": "https://jobs.ashbyhq.com/perplexity",
        "adapter": "ashby", "source_key": "perplexity", "company_size": "startup",
        "user_added": False, "enabled": True,
        "coverage": "Ashby 官方公开 Job Board API；包含当前公开职位",
    },
    "langchain-ashby": {
        "name": "LangChain 官方职位", "company": "LangChain",
        "official_url": "https://jobs.ashbyhq.com/langchain",
        "adapter": "ashby", "source_key": "langchain", "company_size": "startup",
        "user_added": False, "enabled": True,
        "coverage": "Ashby 官方公开 Job Board API；包含当前公开职位",
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
    ("后端开发", ("后端开发", "后端研发", "backend")),
    ("机器学习", ("机器学习", "machine learning")),
    ("深度学习", ("深度学习", "deep learning")),
    ("多模态", ("多模态", "multimodal")), ("AIGC", ("aigc",)),
    ("TypeScript", ("typescript",)), ("React", ("react",)),
)
ROLE_TERMS = (
    "Agent", "智能体", "算法", "大模型", "后端", "全栈", "测试开发",
    "机器学习", "多模态", "产品", "研发", "软件工程", "AI",
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _source_configs() -> dict[str, dict[str, Any]]:
    configs = {key: dict(value) for key, value in SOURCE_CONFIGS.items()}
    for payload in list_custom_job_sources():
        source_id = str(payload.get("id") or "")
        if not source_id:
            continue
        config = dict(payload)
        config.pop("id", None)
        configs[source_id] = config
    return configs


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
    graduate = next((item for item in configs if item.get("recruitType") == "GRADUATE"), {})
    text = " ".join(str(graduate.get(key, "")) for key in ("subtitle", "content"))
    dates = re.findall(r"(20\d{2})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})", text)
    if len(dates) >= 2:
        normalized = [f"{year}-{int(month):02d}-{int(day):02d}" for year, month, day in dates[:2]]
        return f"{normalized[0]} 至 {normalized[1]}"
    return str(graduate.get("subtitle") or "以官网当前校招说明为准")


def _locations(value: str | list[Any]) -> list[str]:
    values = value if isinstance(value, list) else re.split(r"[，/、;；]|,(?=\s*[\u4e00-\u9fff])", value or "")
    locations: list[str] = []
    for item in values:
        if isinstance(item, dict):
            item = item.get("location") or item.get("name") or item.get("city") or ""
        normalized = re.sub(r"市$", "", str(item).strip())
        if normalized and normalized not in locations:
            locations.append(normalized)
    return locations


def _education_requirement(value: str) -> str:
    for line in re.split(r"[\r\n]+", value or ""):
        line = re.sub(r"^[\s\-•·\d.、]+", "", line).strip()
        if line and re.search(r"本科|硕士|博士|学历|bachelor|master|phd|degree", line, re.I):
            return line[:220]
    return "以职位详情为准"


def _plain(value: Any) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _terms(text: str, candidates: tuple[str, ...], limit: int) -> list[str]:
    lowered = text.casefold()
    return [term for term in candidates if term.casefold() in lowered][:limit]


def _skill_terms(text: str, limit: int = 7) -> list[str]:
    lowered = text.casefold()
    return [canonical for canonical, aliases in SKILL_ALIASES
            if any(alias.casefold() in lowered for alias in aliases)][:limit]


def _recruitment_type(title: str, description: str) -> str:
    corpus = f"{title} {description}"
    if re.search(r"校招|校园|应届|20\d{2}届|new\s*grad|graduate|university|campus", corpus, re.I):
        return "校园招聘"
    if re.search(r"实习|\bintern(?:ship)?\b", corpus, re.I):
        return "实习招聘"
    return "社会招聘"


def _generic_graduation(text: str) -> str:
    years = re.findall(r"20(?:2[5-9]|3\d)", text)
    if years and re.search(r"届|毕业|graduate|graduation", text, re.I):
        return f"面向 {max(years)} 届；以职位详情为准"
    return "以职位详情为准"


def _stable_job_id(source_id: str, external_id: Any, title: str, url: str) -> str:
    identity = str(external_id or url or title)
    digest = hashlib.sha256(f"{source_id}:{identity}".encode()).hexdigest()[:18]
    return f"{source_id}-{digest}"


def _generic_job(config: dict[str, Any], source_id: str, *, external_id: Any,
                 title: Any, locations: str | list[Any], description: Any, url: Any,
                 discovered_at: datetime, updated_at: Any = "", job_code: Any = "",
                 department: Any = "", evidence: list[str] | None = None) -> JobPosting | None:
    clean_title = _plain(title)
    clean_url = str(url or config["official_url"]).strip()
    clean_description = _plain(description)
    if not clean_title or not clean_url.startswith("https://"):
        return None
    corpus = f"{clean_title} {clean_description} {_plain(department)}"
    return JobPosting(
        id=_stable_job_id(source_id, external_id, clean_title, clean_url),
        company=str(config["company"]), title=clean_title,
        job_code=str(job_code or external_id or "")[:160], locations=_locations(locations),
        recruitment_type=_recruitment_type(clean_title, clean_description),
        graduation_window=_generic_graduation(corpus),
        education_requirement=_education_requirement(clean_description),
        description=clean_description[:1800], required_skills=_skill_terms(corpus),
        preferred_skills=[], role_keywords=_terms(corpus, ROLE_TERMS, 7),
        url=clean_url, source_name=str(config["name"]), source_url=clean_url,
        source_status="verify_on_open", apply_mode="direct",
        discovery_source=source_id, discovered_at=discovered_at.isoformat(),
        source_updated_at=str(updated_at or ""), discovery_scope=str(config["coverage"]),
        discovery_evidence=evidence or [f"{config['adapter']} 官方公开职位源"],
        company_size=config.get("company_size", "unknown"),
    )


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
        id=f"baidu-{job_code.casefold() if job_code else identifier}", company="百度",
        title=clean_title, job_code=job_code,
        locations=_locations(str(record.get("workPlace") or "")), recruitment_type="校园招聘",
        graduation_window=graduation_window, education_requirement=_education_requirement(requirements),
        description=work[:1600], required_skills=_skill_terms(corpus), preferred_skills=[],
        role_keywords=_terms(corpus, ROLE_TERMS, 6), url=detail_url,
        source_name="百度校园招聘官网", source_url=detail_url,
        source_status="verify_on_open", apply_mode="direct",
        discovery_source="baidu-campus", discovered_at=discovered_at.isoformat(),
        source_updated_at=updated_at, discovery_scope=scope, discovery_evidence=evidence,
        company_size="large",
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
    scope = f"官网公开列表第 {page_num} 页，扫描 {len(records)} 条"
    if total is not None:
        scope += f" / 共 {total} 条"
    window = _graduation_window(list_data)
    jobs = [job for record in records
            if (job := _baidu_job(record, window, timestamp, scope)) is not None]
    return jobs, total, max(0, len(records) - len(jobs))


def _parse_greenhouse(payload: Any, config: dict[str, Any], source_id: str,
                      timestamp: datetime) -> tuple[list[JobPosting], int | None, int]:
    records = payload.get("jobs", []) if isinstance(payload, dict) else []
    jobs: list[JobPosting] = []
    for record in records:
        location = (record.get("location") or {}).get("name", "")
        departments = ", ".join(item.get("name", "") for item in record.get("departments", []))
        job = _generic_job(
            config, source_id, external_id=record.get("id"), title=record.get("title"),
            locations=location, description=record.get("content"), url=record.get("absolute_url"),
            discovered_at=timestamp, updated_at=record.get("updated_at"), department=departments,
            evidence=["Greenhouse 官方 Job Board API"],
        )
        if job:
            jobs.append(job)
    return jobs, len(records), max(0, len(records) - len(jobs))


def _parse_lever(payload: Any, config: dict[str, Any], source_id: str,
                 timestamp: datetime) -> tuple[list[JobPosting], int | None, int]:
    records = payload if isinstance(payload, list) else []
    jobs: list[JobPosting] = []
    for record in records:
        categories = record.get("categories") or {}
        description = " ".join(str(record.get(key) or "") for key in (
            "descriptionPlain", "additionalPlain", "description", "additional",
        ))
        job = _generic_job(
            config, source_id, external_id=record.get("id"), title=record.get("text"),
            locations=categories.get("location", ""), description=description,
            url=record.get("hostedUrl") or record.get("applyUrl"), discovered_at=timestamp,
            updated_at=record.get("createdAt"), department=categories.get("team", ""),
            evidence=["Lever 官方 Postings API"],
        )
        if job:
            jobs.append(job)
    return jobs, len(records), max(0, len(records) - len(jobs))


def _parse_ashby(payload: Any, config: dict[str, Any], source_id: str,
                 timestamp: datetime) -> tuple[list[JobPosting], int | None, int]:
    records = payload.get("jobs", []) if isinstance(payload, dict) else []
    jobs: list[JobPosting] = []
    skipped = 0
    for record in records:
        if record.get("isListed") is False:
            skipped += 1
            continue
        locations: list[Any] = [record.get("location", ""), *(record.get("secondaryLocations") or [])]
        job = _generic_job(
            config, source_id, external_id=record.get("id") or record.get("jobUrl"),
            title=record.get("title"), locations=locations,
            description=record.get("descriptionHtml") or record.get("descriptionPlain"),
            url=record.get("jobUrl") or record.get("applyUrl"), discovered_at=timestamp,
            updated_at=record.get("publishedAt"),
            department=record.get("department") or record.get("team"),
            evidence=["Ashby 官方 Public Job Posting API"],
        )
        if job:
            jobs.append(job)
        else:
            skipped += 1
    return jobs, len(records), skipped


def _parse_smartrecruiters(payload: Any, config: dict[str, Any], source_id: str,
                           timestamp: datetime) -> tuple[list[JobPosting], int | None, int]:
    records = payload.get("content", []) if isinstance(payload, dict) else []
    total = payload.get("totalFound") if isinstance(payload, dict) else None
    total = int(total) if str(total).isdigit() else len(records)
    jobs: list[JobPosting] = []
    for record in records:
        location_data = record.get("location") or {}
        location = ", ".join(filter(None, [location_data.get("city"), location_data.get("region"),
                                            location_data.get("country")]))
        department = (record.get("department") or {}).get("label", "")
        external_id = record.get("id") or record.get("refNumber")
        slug = re.sub(r"[^a-z0-9]+", "-", str(record.get("name") or "").casefold()).strip("-")
        url = record.get("ref") or f"https://jobs.smartrecruiters.com/{config['source_key']}/{external_id}-{slug}"
        job = _generic_job(
            config, source_id, external_id=external_id, title=record.get("name"), locations=location,
            description=record.get("jobAd") or department, url=url, discovered_at=timestamp,
            updated_at=record.get("releasedDate"), job_code=record.get("refNumber"),
            department=department, evidence=["SmartRecruiters 官方 Posting API"],
        )
        if job:
            jobs.append(job)
    return jobs, total, max(0, len(records) - len(jobs))


def _jsonld_objects(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        types = value.get("@type", [])
        types = types if isinstance(types, list) else [types]
        if any(str(item).casefold() == "jobposting" for item in types):
            result.append(value)
        for nested in value.values():
            result.extend(_jsonld_objects(nested))
    elif isinstance(value, list):
        for nested in value:
            result.extend(_jsonld_objects(nested))
    return result


def _jsonld_locations(value: Any) -> list[str]:
    values = value if isinstance(value, list) else [value]
    result: list[str] = []
    for location in values:
        if not isinstance(location, dict):
            continue
        address = location.get("address") or {}
        if isinstance(address, str):
            text = address
        else:
            text = ", ".join(filter(None, [address.get("addressLocality"),
                                            address.get("addressRegion"), address.get("addressCountry")]))
        if text:
            result.append(text)
    return result


def _parse_jsonld(content: str, config: dict[str, Any], source_id: str,
                  timestamp: datetime) -> tuple[list[JobPosting], int | None, int]:
    records: list[dict[str, Any]] = []
    for raw in re.findall(r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
                          content, flags=re.I | re.S):
        try:
            records.extend(_jsonld_objects(json.loads(html.unescape(raw.strip()))))
        except (json.JSONDecodeError, TypeError):
            continue
    jobs: list[JobPosting] = []
    for record in records:
        identifier = record.get("identifier")
        if isinstance(identifier, dict):
            identifier = identifier.get("value") or identifier.get("name")
        job = _generic_job(
            config, source_id, external_id=identifier, title=record.get("title"),
            locations=_jsonld_locations(record.get("jobLocation")),
            description=record.get("description"), url=record.get("url") or config["official_url"],
            discovered_at=timestamp, updated_at=record.get("datePosted"), job_code=identifier,
            evidence=["招聘官网 Schema.org JobPosting 结构化数据"],
        )
        if job:
            jobs.append(job)
    return jobs, len(records), max(0, len(records) - len(jobs))


def _check_official_response(response: httpx.Response, official_url: str) -> None:
    if response.status_code >= 400:
        raise ValueError(f"官网返回 HTTP {response.status_code}")
    expected_host = (urlparse(official_url).hostname or "").lower()
    if any((item.url.scheme != "https" or (item.url.host or "").lower() != expected_host)
           for item in [*response.history, response]):
        raise ValueError("官网请求发生了未授权的跨域重定向")
    if len(response.content) > MAX_SOURCE_BYTES:
        raise ValueError("官网数据超过安全读取上限")


def _validate_source_url(raw_url: str) -> str:
    value = raw_url.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("招聘来源必须是公开的 HTTPS 官网地址")
    host = parsed.hostname.casefold().rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("招聘来源不能指向本机或内网")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return value
    if not address.is_global:
        raise ValueError("招聘来源不能指向本机或内网")
    return value


async def _assert_public_host(url: str) -> None:
    host = urlparse(url).hostname or ""
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("无法解析招聘官网域名") from exc
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise ValueError("招聘来源解析到了本机或内网地址")


def _adapter_and_key(url: str, requested: DiscoveryAdapter,
                     explicit_key: str = "") -> tuple[DiscoveryAdapter, str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    parts = [item for item in parsed.path.split("/") if item]
    detected: DiscoveryAdapter = requested
    if requested == "auto":
        if "greenhouse.io" in host:
            detected = "greenhouse"
        elif "lever.co" in host:
            detected = "lever"
        elif "ashbyhq.com" in host:
            detected = "ashby"
        elif "smartrecruiters.com" in host:
            detected = "smartrecruiters"
        elif host == "talent.baidu.com":
            detected = "baidu"
        else:
            detected = "jsonld"
    key = explicit_key.strip()
    if not key and detected in {"greenhouse", "lever", "ashby", "smartrecruiters"}:
        ignored = {"job", "jobs", "postings", "companies", "v0", "v1", "job-board", "posting-api"}
        key = next((part for part in parts if part.casefold() not in ignored), "")
    if detected in {"greenhouse", "lever", "ashby", "smartrecruiters"} and not key:
        raise ValueError("无法从网址识别 ATS 公司标识，请手动填写 source key")
    return detected, key


def register_job_source(payload: JobSourceCreate) -> OfficialJobSource:
    official_url = _validate_source_url(payload.official_url)
    adapter, source_key = _adapter_and_key(official_url, payload.adapter, payload.source_key)
    user_id = current_user_id()
    slug = re.sub(r"[^a-z0-9]+", "-", payload.company.casefold()).strip("-")[:28] or "company"
    digest = hashlib.sha256(f"{user_id}:{official_url}:{source_key}".encode()).hexdigest()[:10]
    source_id = f"custom-{slug}-{digest}"
    adapter_labels = {
        "greenhouse": "Greenhouse 官方公开 Job Board API",
        "lever": "Lever 官方公开 Postings API",
        "ashby": "Ashby 官方公开 Job Posting API",
        "smartrecruiters": "SmartRecruiters 官方公开 Posting API",
        "jsonld": "招聘官网 Schema.org JobPosting 结构化数据",
        "baidu": "百度招聘官网公开职位",
    }
    config = {
        "id": source_id, "name": f"{payload.company}官方职位", "company": payload.company.strip(),
        "official_url": official_url, "adapter": adapter, "source_key": source_key,
        "company_size": payload.company_size, "user_added": True, "enabled": True,
        "coverage": adapter_labels.get(adapter, "招聘官网公开职位"),
    }
    save_custom_job_source(config)
    return OfficialJobSource(**config)


def remove_job_source(source_id: str) -> bool:
    if source_id in SOURCE_CONFIGS:
        raise ValueError("内置招聘源不能删除")
    return delete_custom_job_source(source_id)


async def _baidu_api_page(client: httpx.AsyncClient, page_number: int,
                          graduation_window: str, discovered_at: datetime
                          ) -> tuple[list[JobPosting], int | None, int, int]:
    response = await client.post(
        "/httservice/getPostListNew",
        headers={"Origin": "https://talent.baidu.com", "Referer": BAIDU_CAMPUS_URL,
                 "X-Requested-With": "XMLHttpRequest"},
        data={"recruitType": "GRADUATE", "pageSize": "10", "keyWord": "",
              "curPage": str(page_number), "projectType": ""},
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


async def _sync_baidu(source_id: str, config: dict[str, Any], started_at: datetime,
                      transport: httpx.AsyncBaseTransport | None
                      ) -> tuple[list[JobPosting], int | None, int, bool, str]:
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=True, base_url="https://talent.baidu.com",
        timeout=httpx.Timeout(30.0, connect=8.0),
        headers={"User-Agent": "Mozilla/5.0 (compatible; ZhidaJobDiscovery/0.2)"},
    ) as client:
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
    partial = bool(page_failure) or total is None or records_scanned < total
    detail = page_failure
    if not detail and total_pages > MAX_SOURCE_PAGES:
        detail = f"达到 {MAX_SOURCE_PAGES} 页安全上限，共 {total} 个"
    elif not detail and partial and total is not None:
        detail = f"当前扫描 {records_scanned}/{total} 个"
    return jobs, total, skipped, partial, detail


def _fetch_url(config: dict[str, Any]) -> str:
    adapter, key = config["adapter"], config.get("source_key", "")
    if adapter == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{key}/jobs?content=true"
    if adapter == "lever":
        return f"https://api.lever.co/v0/postings/{key}?mode=json"
    if adapter == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{key}?includeCompensation=false"
    if adapter == "smartrecruiters":
        return f"https://api.smartrecruiters.com/v1/companies/{key}/postings?limit=100&offset=0"
    return str(config["official_url"])


async def _sync_standard(source_id: str, config: dict[str, Any], started_at: datetime,
                         transport: httpx.AsyncBaseTransport | None
                         ) -> tuple[list[JobPosting], int | None, int, bool, str]:
    fetch_url = _fetch_url(config)
    if transport is None and config["adapter"] == "jsonld":
        await _assert_public_host(fetch_url)
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=True, timeout=httpx.Timeout(45.0, connect=10.0),
        headers={"User-Agent": "Mozilla/5.0 (compatible; ZhidaJobDiscovery/0.2)"},
    ) as client:
        response = await client.get(fetch_url)
    _check_official_response(response, fetch_url)
    adapter = config["adapter"]
    if adapter == "jsonld":
        jobs, total, skipped = _parse_jsonld(response.text, config, source_id, started_at)
    else:
        payload = response.json()
        parsers = {
            "greenhouse": _parse_greenhouse, "lever": _parse_lever,
            "ashby": _parse_ashby, "smartrecruiters": _parse_smartrecruiters,
        }
        parser = parsers.get(adapter)
        if not parser:
            raise ValueError("该招聘来源适配器尚未实现")
        jobs, total, skipped = parser(payload, config, source_id, started_at)
    partial = total is None or len(jobs) + skipped < total
    detail = f"当前读取 {len(jobs) + skipped}/{total} 个" if partial and total is not None else ""
    return jobs, total, skipped, partial, detail


def official_job_sources() -> list[OfficialJobSource]:
    result: list[OfficialJobSource] = []
    for source_id, config in _source_configs().items():
        run = get_job_source_run(source_id) or {}
        result.append(OfficialJobSource(
            id=source_id, **config, last_status=run.get("status", "never"),
            last_completed_at=run.get("completed_at", ""),
            last_message=run.get("message", "尚未同步"), jobs_seen=run.get("jobs_seen", 0),
            total_available=run.get("total_available"), partial=run.get("partial", True),
        ))
    return result


def _save_run(result: JobDiscoveryResult) -> None:
    save_job_source_run({
        **result.model_dump(exclude={"jobs"}), "started_at": result.started_at.isoformat(),
        "completed_at": result.completed_at.isoformat(),
    })


async def sync_official_source(source_id: str,
                               transport: httpx.AsyncBaseTransport | None = None
                               ) -> JobDiscoveryResult:
    config = _source_configs().get(source_id)
    if not config:
        raise LookupError("未知的官方岗位来源")
    started_at = _now()
    try:
        if config["adapter"] == "baidu":
            jobs, total, skipped, partial, detail = await _sync_baidu(
                source_id, config, started_at, transport
            )
        else:
            jobs, total, skipped, partial, detail = await _sync_standard(
                source_id, config, started_at, transport
            )
        if not jobs:
            raise ValueError("官网没有解析出可用的公开岗位")
        jobs = list({job.id: job for job in jobs}.values())
        completed_at = _now()
        created, updated = save_discovered_jobs(
            source_id, [job.model_dump(mode="json") for job in jobs], completed_at.isoformat(),
            replace_missing=not partial,
        )
        status = "partial" if partial else "success"
        message = f"从官网同步 {len(jobs)} 个岗位"
        if detail:
            message += f"（{detail}）"
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
