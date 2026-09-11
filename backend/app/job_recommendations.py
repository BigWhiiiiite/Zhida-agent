from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from pydantic import ValidationError

from .job_models import (ApplicationQueueItem, JobPosting, JobRecommendation,
                         JobVerification, QueueAddRequest, RecommendationBatch)
from .models import CandidateProfile
from .storage import (add_job_queue_entries, delete_job_queue_entry, get_job_verification,
                      get_resume, list_discovered_jobs, list_job_queue_entries)


# This seed catalog keeps the recommendation-to-autofill loop usable while live
# discovery adapters are being built. Every item retains its official source and
# verification state; a match score never claims that an opening is still live.
JOB_CATALOG: tuple[JobPosting, ...] = (
    JobPosting(
        id="baidu-j99969-agent-algorithm",
        company="百度",
        title="2027AIDU-智能体算法工程师",
        job_code="J99969",
        locations=["北京"],
        graduation_window="2026-09-01 至 2027-08-31",
        education_requirement="硕士及以上，计算机、人工智能等相关专业",
        description="设计和研发 AI Agent，覆盖规划、工具调用、反思、多智能体协作、RAG 与评测体系。",
        required_skills=["Agent", "大语言模型", "Python", "LangChain"],
        preferred_skills=["AutoGen", "RAG", "ReAct", "评测"],
        role_keywords=["智能体", "Agent", "算法", "大模型"],
        url="https://talent.baidu.com/jobs/detail/GRADUATE/4f1cbc80-8332-4a92-b8fa-c0132b17d47e",
        source_name="百度校园招聘官网",
        source_url="https://talent.baidu.com/jobs/detail/GRADUATE/4f1cbc80-8332-4a92-b8fa-c0132b17d47e",
        source_status="verified",
        apply_mode="direct",
        verified_at="2026-09-11",
    ),
    JobPosting(
        id="baidu-j101017-agent-algorithm",
        company="百度",
        title="北京-智能体算法工程师",
        job_code="J101017",
        locations=["北京"],
        graduation_window="2026-09-01 至 2027-08-31",
        education_requirement="本科及以上，计算机、人工智能、软件工程等相关专业",
        description="负责大模型智能体系统设计与开发，包括任务规划、工具调用、记忆管理、多智能体协同和评估体系。",
        required_skills=["Agent", "大语言模型", "Python", "LangChain"],
        preferred_skills=["PyTorch", "AutoGen", "ReAct", "评测"],
        role_keywords=["智能体", "Agent", "算法", "大模型"],
        url="https://talent.baidu.com/jobs/detail/GRADUATE/02f73086-be71-4d09-8d6e-f1c6981b8b48",
        source_name="百度校园招聘官网",
        source_url="https://talent.baidu.com/jobs/detail/GRADUATE/02f73086-be71-4d09-8d6e-f1c6981b8b48",
        source_status="verified",
        apply_mode="direct",
        verified_at="2026-09-11",
    ),
    JobPosting(
        id="baidu-j99974-agent-fullstack",
        company="百度",
        title="2027AIDU-Agent应用全栈工程师",
        job_code="J99974",
        locations=["北京"],
        graduation_window="2026-09-01 至 2027-08-31",
        education_requirement="本科及以上，计算机或软件工程相关专业",
        description="构建基于大模型的 Autonomous Agent 系统，推进 Agent 应用的前后端工程化落地。",
        required_skills=["Agent", "Python", "后端开发", "大语言模型"],
        preferred_skills=["FastAPI", "React", "RAG", "Tool Calling"],
        role_keywords=["Agent", "全栈", "大模型", "应用研发"],
        url="https://talent.baidu.com/jobs/detail/GRADUATE/6f9c3a86-6557-409d-8fa7-e6f4c68d6765",
        source_name="百度校园招聘官网",
        source_url="https://talent.baidu.com/jobs/detail/GRADUATE/6f9c3a86-6557-409d-8fa7-e6f4c68d6765",
        source_status="verified",
        apply_mode="direct",
        verified_at="2026-09-11",
    ),
    JobPosting(
        id="baidu-j100737-backend",
        company="百度",
        title="北京-后端开发工程师",
        job_code="J100737",
        locations=["北京"],
        graduation_window="2026-09-01 至 2027-08-31",
        education_requirement="本科及以上，计算机或软件工程相关专业",
        description="负责核心产品与架构开发，将 AI 大模型能力集成到业务产品，并保障系统稳定性。",
        required_skills=["后端开发", "数据结构", "Linux"],
        preferred_skills=["Python", "Java", "Go", "分布式系统"],
        role_keywords=["后端", "研发", "软件工程"],
        url="https://talent.baidu.com/jobs/list",
        source_name="百度校园招聘官网",
        source_url="https://talent.baidu.com/jobs/list",
        source_status="verified",
        apply_mode="search",
        verified_at="2026-09-10",
    ),
    JobPosting(
        id="baidu-j101055-ai-test",
        company="百度",
        title="北京-AI测试开发工程师",
        job_code="J101055",
        locations=["北京"],
        graduation_window="2026-09-01 至 2027-08-31",
        education_requirement="本科及以上，计算机相关专业",
        description="研发 AI 智能化测试平台，覆盖 Agent 编排、模型评测、自动化测试和质量体系建设。",
        required_skills=["Python", "自动化测试", "Agent"],
        preferred_skills=["大语言模型", "评测", "FastAPI", "CI/CD"],
        role_keywords=["测试开发", "Agent", "评测", "研发"],
        url="https://talent.baidu.com/jobs/list",
        source_name="百度校园招聘官网",
        source_url="https://talent.baidu.com/jobs/list",
        source_status="verified",
        apply_mode="search",
        verified_at="2026-09-10",
    ),
    JobPosting(
        id="tencent-1282707395466077184",
        company="腾讯",
        title="Agent 开发方向校招岗位",
        job_code="1282707395466077184",
        locations=["深圳"],
        graduation_window="以官网当前页面为准",
        education_requirement="以职位详情为准",
        description="用户提供的腾讯校园招聘职位，用于验证登录、在线简历和自动填写完整流程。",
        required_skills=["Agent", "Python", "大语言模型"],
        preferred_skills=["FastAPI", "多智能体", "Tool Calling"],
        role_keywords=["Agent", "智能体", "应用研发"],
        url="https://join.qq.com/post_detail.html?postid=1282707395466077184",
        source_name="腾讯校园招聘官网",
        source_url="https://join.qq.com/post_detail.html?postid=1282707395466077184",
        source_status="verify_on_open",
        apply_mode="direct",
    ),
    JobPosting(
        id="bytedance-frontier-ai-entry",
        company="字节跳动",
        title="前沿技术领域人才校招（Agent/大模型岗位入口）",
        job_code="project-entry",
        locations=["北京", "上海", "深圳", "杭州"],
        graduation_window="2027届及以后，具体岗位以官网为准",
        education_requirement="以具体职位为准",
        description="字节跳动前沿技术领域人才校招入口，包含 Agent、基础大模型、AI 搜索和机器学习系统等方向。",
        required_skills=["大语言模型", "Python"],
        preferred_skills=["Agent", "PyTorch", "机器学习"],
        role_keywords=["Agent", "大模型", "算法", "研发"],
        url="https://jobs.bytedance.com/campus/page-6272Gc",
        source_name="字节跳动校园招聘官网",
        source_url="https://jobs.bytedance.com/campus/page-6272Gc",
        source_status="verify_on_open",
        apply_mode="search",
    ),
)

VERIFICATION_TTL = timedelta(hours=6)


def all_jobs() -> tuple[JobPosting, ...]:
    """Merge curated metadata with official discoveries without duplicating job codes."""
    jobs = list(JOB_CATALOG)
    positions = {
        (job.company.casefold(), job.job_code.casefold()): index
        for index, job in enumerate(jobs) if job.job_code
    }
    for payload in list_discovered_jobs():
        try:
            discovered = JobPosting.model_validate(payload)
        except ValidationError:
            continue
        key = (discovered.company.casefold(), discovered.job_code.casefold())
        if discovered.job_code and key in positions:
            index = positions[key]
            curated = jobs[index]
            jobs[index] = curated.model_copy(update={
                "url": discovered.url,
                "source_url": discovered.source_url,
                "discovery_source": discovered.discovery_source,
                "discovered_at": discovered.discovered_at,
                "source_updated_at": discovered.source_updated_at,
                "discovery_scope": discovered.discovery_scope,
                "discovery_evidence": discovered.discovery_evidence,
            })
            continue
        positions[key] = len(jobs)
        jobs.append(discovered)
    return tuple(jobs)


ALIASES: dict[str, tuple[str, ...]] = {
    "Agent": ("agent", "智能体", "multi-agent", "multi agent", "agent workflow"),
    "大语言模型": ("大语言模型", "大模型", "llm", "aigc"),
    "Python": ("python",),
    "FastAPI": ("fastapi",),
    "React": ("react",),
    "LangChain": ("langchain",),
    "AutoGen": ("autogen",),
    "ReAct": ("react范式", "react agent", "reasoning and acting"),
    "RAG": ("rag", "检索增强"),
    "Tool Calling": ("tool calling", "工具调用", "function calling"),
    "后端开发": ("后端", "backend", "服务端"),
    "多智能体": ("多智能体", "multi-agent", "multi agent"),
    "评测": ("评测", "evaluation", "eval"),
    "自动化测试": ("自动化测试", "pytest", "playwright", "测试开发"),
    "CI/CD": ("ci/cd", "cicd", "持续集成", "持续交付"),
    "数据结构": ("数据结构", "algorithm", "算法"),
    "Linux": ("linux",),
    "Java": ("java",),
    "Go": ("golang", " go "),
    "分布式系统": ("分布式", "distributed"),
    "PyTorch": ("pytorch",),
    "机器学习": ("机器学习", "machine learning"),
}


def _profile_corpus(profile: CandidateProfile) -> str:
    chunks: list[str] = [profile.target_role, profile.summary, " ".join(profile.skills)]
    for education in profile.education:
        chunks.extend([education.school, education.college, education.degree, education.major,
                       " ".join(education.courses), education.description])
    for experience in profile.internships:
        chunks.extend([experience.organization, experience.department, experience.role,
                       experience.description, " ".join(experience.achievements),
                       " ".join(experience.technologies)])
    for project in profile.projects:
        chunks.extend([project.name, project.role, project.background, project.description,
                       " ".join(project.achievements), " ".join(project.technologies)])
    return " ".join(chunk for chunk in chunks if chunk).lower()


def _has_skill(skill: str, corpus: str) -> bool:
    terms = ALIASES.get(skill, (skill.lower(),))
    return any(term.lower() in corpus for term in terms)


def _graduation_year(profile: CandidateProfile) -> int | None:
    years: list[int] = []
    for education in profile.education:
        years.extend(int(year) for year in re.findall(r"20\d{2}", education.end_date))
    return max(years) if years else None


def _graduation_date(profile: CandidateProfile) -> date | None:
    dates: list[date] = []
    for education in profile.education:
        match = re.search(r"(20\d{2})(?:[-./年](\d{1,2}))?", education.end_date)
        if not match:
            continue
        year = int(match.group(1))
        month = int(match.group(2) or 6)
        if 1 <= month <= 12:
            dates.append(date(year, month, 1))
    return max(dates) if dates else None


def _match_graduation(profile: CandidateProfile, job: JobPosting) -> bool | None:
    graduation = _graduation_date(profile)
    window_dates = re.findall(r"(20\d{2})-(\d{2})-(\d{2})", job.graduation_window)
    if graduation is None or len(window_dates) < 2:
        return None
    start_parts, end_parts = window_dates[0], window_dates[1]
    start = date(*(int(part) for part in start_parts))
    end = date(*(int(part) for part in end_parts))
    return start <= graduation <= end


def _normalize_location(value: str) -> str:
    return value.replace("市", "").strip().lower()


def _match_location(profile: CandidateProfile, job: JobPosting, preferred_location: str = "") -> bool | None:
    selected = _normalize_location(preferred_location)
    targets = ([selected] if selected else
               [_normalize_location(item) for item in profile.target_cities if item.strip()])
    if not targets:
        return None
    locations = [_normalize_location(item) for item in job.locations]
    return any(target in location or location in target for target in targets for location in locations)


def _fresh_verification(record: dict | None) -> bool:
    if not record:
        return False
    try:
        checked_at = datetime.fromisoformat(str(record["checked_at"]).replace("Z", "+00:00"))
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
    except (KeyError, TypeError, ValueError):
        return False
    age = datetime.now(timezone.utc) - checked_at.astimezone(timezone.utc)
    return timedelta(0) <= age <= VERIFICATION_TTL


def _score(profile: CandidateProfile, job: JobPosting, preferred_location: str = "") -> JobRecommendation:
    verification = get_job_verification(job.id)
    if (_fresh_verification(verification)
            and verification.get("official_url") == job.source_url):
        job = job.model_copy(update={
            "live_status": verification["status"],
            "last_checked_at": verification["checked_at"],
            "verification_message": verification["message"],
            "verification_evidence": verification["evidence"],
        })
    elif verification:
        job = job.model_copy(update={
            "last_checked_at": verification["checked_at"],
            "verification_message": "上次官网核验已超过 6 小时，请重新核验",
        })
    corpus = _profile_corpus(profile)
    matched_required = [skill for skill in job.required_skills if _has_skill(skill, corpus)]
    matched_preferred = [skill for skill in job.preferred_skills if _has_skill(skill, corpus)]
    missing = [skill for skill in job.required_skills if skill not in matched_required]
    matched = list(dict.fromkeys(matched_required + matched_preferred))

    target = profile.target_role.lower()
    role_match = bool(target and any(keyword.lower() in target or keyword.lower() in corpus
                                     for keyword in job.role_keywords))
    location_match = _match_location(profile, job, preferred_location)
    graduation_match = _match_graduation(profile, job)

    required_ratio = len(matched_required) / max(1, len(job.required_skills))
    preferred_ratio = len(matched_preferred) / max(1, len(job.preferred_skills))
    score = 20 + round(required_ratio * 35) + round(preferred_ratio * 15)
    score += 15 if role_match else 0
    score += 4 if location_match is None else (8 if location_match else 0)
    score += 3 if graduation_match is None else (7 if graduation_match else 0)
    score = max(0, min(100, score))

    reasons: list[str] = []
    if matched:
        reasons.append(f"简历已体现 {', '.join(matched[:4])}")
    if role_match:
        reasons.append("目标岗位与该职位方向一致")
    if location_match:
        reasons.append(f"工作地点包含所选城市：{preferred_location}" if preferred_location else
                       "工作地点符合目标城市")
    if graduation_match:
        reasons.append("毕业时间符合 2027 届校招窗口")
    if missing:
        reasons.append(f"投递前建议补充或核实：{', '.join(missing[:3])}")
    if not reasons:
        reasons.append("资料信号较少，建议先完善目标岗位和技能")

    algorithm_heavy = "算法" in job.title or "AIDU" in job.title
    track = "stretch" if algorithm_heavy or len(missing) >= 2 else "steady"
    gate_reasons: list[str] = []
    if job.live_status != "open":
        gate_reasons.append("官网尚未实时确认为可投")
    if graduation_match is not True:
        gate_reasons.append("毕业时间尚未确认符合届别")
    if location_match is not True:
        gate_reasons.append("工作地点尚未确认符合偏好")
    if re.search(r"实习|intern|social|社招", job.recruitment_type, re.I):
        gate_reasons.append("不属于正式校招岗位")
    return JobRecommendation(job=job, match_score=score, matched_skills=matched,
                             missing_skills=missing, reasons=reasons,
                             location_match=location_match, graduation_match=graduation_match,
                             queue_track=track, formal_queue_eligible=not gate_reasons,
                             gate_reasons=gate_reasons)


def recommendation_batch(profile: CandidateProfile, preferred_location: str = "") -> RecommendationBatch:
    catalog = all_jobs()
    available_locations = list(dict.fromkeys(
        location for job in catalog for location in job.locations if location.strip()
    ))
    selected_location = next(
        (location for location in available_locations
         if _normalize_location(location) == _normalize_location(preferred_location)), ""
    ) if preferred_location.strip() else ""
    recommendations = [_score(profile, job, selected_location) for job in catalog]
    if selected_location:
        recommendations = [item for item in recommendations if item.location_match is True]
    jobs = sorted(recommendations,
                  key=lambda item: (item.formal_queue_eligible, item.match_score,
                                    item.job.source_status == "verified"), reverse=True)
    year = _graduation_year(profile)
    summary_parts = [profile.target_role or "未设置目标岗位", f"{len(profile.skills)} 项技能"]
    if year:
        summary_parts.append(f"预计 {year} 年毕业")
    if selected_location:
        summary_parts.append(f"工作地点：{selected_location}")
    return RecommendationBatch(generated_at=datetime.now(timezone.utc), engine="official-discovery-verified-local-score-v3",
                               profile_summary=" · ".join(summary_parts),
                               available_locations=available_locations,
                               selected_location=selected_location, jobs=jobs)


def _recommendation_by_id(profile: CandidateProfile, job_id: str) -> JobRecommendation | None:
    return next((item for item in recommendation_batch(profile).jobs if item.job.id == job_id), None)


def catalog_job(job_id: str) -> JobPosting | None:
    return next((job for job in all_jobs() if job.id == job_id), None)


async def verify_catalog_job(job_id: str) -> JobVerification:
    job = catalog_job(job_id)
    if not job:
        raise LookupError("岗位不存在")
    from .job_verification import verify_official_job
    return await verify_official_job(job)


def queue_items(profile: CandidateProfile) -> list[ApplicationQueueItem]:
    items: list[ApplicationQueueItem] = []
    for record in list_job_queue_entries():
        recommendation = _recommendation_by_id(profile, record["job_id"])
        if recommendation:
            items.append(ApplicationQueueItem(**record, recommendation=recommendation))
    return items


def add_to_queue(profile: CandidateProfile, payload: QueueAddRequest) -> list[ApplicationQueueItem]:
    known = {job.id for job in all_jobs()}
    unknown = [job_id for job_id in payload.job_ids if job_id not in known]
    if unknown:
        raise ValueError(f"岗位不存在：{', '.join(unknown)}")
    if payload.resume_id and not get_resume(payload.resume_id):
        raise LookupError("选择的简历不存在")
    add_job_queue_entries(list(dict.fromkeys(payload.job_ids)), payload.resume_id)
    return queue_items(profile)


def remove_from_queue(queue_id: str) -> bool:
    return delete_job_queue_entry(queue_id)
