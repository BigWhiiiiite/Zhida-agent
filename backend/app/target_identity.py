"""Conservative, side-effect-free checks for an already selected job.

Missing or broad evidence is unknown, not a successful identity match. These
checks are write blockers only; they do not grant navigation or filling rights.
"""
from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from .job_navigation import job_identity

if TYPE_CHECKING:
    from .application_models import ApplicationWorkflowState


CHECKED_STAGES = frozenset({"job_detail", "profile_form", "application_form", "review"})
GENERIC_TITLES = frozenset({
    "ai", "agent", "aiagent", "人工智能", "智能体", "算法", "开发", "研发", "技术", "软件", "数据",
    "工程师", "实习", "实习生", "校招", "职位详情", "岗位详情", "申请表", "在线简历", "填写简历",
    "个人信息", "基本信息", "我的简历", "application", "applicationform", "jobdetails", "careers",
    "engineer", "developer", "engineering", "intern", "internship",
})


def _origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlparse(value)
        scheme, host = parsed.scheme.casefold(), (parsed.hostname or "").casefold().rstrip(".")
        if scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
            return None
        return scheme, host, parsed.port or (443 if scheme == "https" else 80)
    except ValueError:
        return None


def _title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = re.sub(r"^(?:您|你)?(?:正在|正)?(?:投递|申请)(?:的)?(?:职位|岗位)\s*[:：]\s*", "", value)
    value = re.sub(r"^(?:职位名称|岗位名称|招聘职位|招聘岗位)\s*[:：]\s*", "", value)
    # Remove only known recruitment decorations, never whole parenthetical
    # phrases: (北京), (算法方向) and C++/C# remain meaningful role evidence.
    value = re.sub(r"(?<!\d)20\d{2}\s*(?:届|级)?\s*(?:校园招聘|校招|春招|秋招|campus\s+recruitment)", "", value)
    value = re.sub(r"^(?:校园招聘|校园|校招|社会招聘|社招|春招|秋招|campus\s+recruitment)\s*[-—_:：|]*\s*", "", value)
    value = re.sub(r"[（(【\[]\s*(?:职位编号\s*[:：]?\s*)?[a-z]{1,4}\d{4,}\s*[）)】\]]", "", value)
    value = re.sub(r"(?:职位编号\s*[:：]\s*)[a-z]{1,4}\d{4,}\s*$", "", value)
    value = re.sub(r"[^a-z0-9\u4e00-\u9fff+#]", "", value)
    value = re.sub(r"(?:岗位|职位|岗)$", "", value)
    return value.replace("人工智能", "ai").replace("智能体", "agent").replace("aiagent", "agent")


def _specific(value: str) -> bool:
    if not value or value in GENERIC_TITLES:
        return False
    han = re.findall(r"[\u4e00-\u9fff]", value)
    # Short intent/search keywords are not an exact selected job title.
    return len(han) >= 4 or bool(re.search(r"engineer|developer|scientist|analyst|manager|researcher", value))


def _identifier(url: str) -> str:
    value = job_identity(url)
    # UUID locators are case-insensitive; other ATS identifiers may not be.
    if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        return value.casefold()
    return value


def target_identity_blocker(workflow: "ApplicationWorkflowState") -> str:
    """Return a user-facing mismatch blocker, or ``""`` when unproven.

    Uses ``workflow.target.source_url/job_title`` and the *observed* page
    ``url/job_title/stage``. Never uses document.title or a model-proposed
    title as evidence. Comparison is restricted to real detail/form/review
    stages. Cross-domain login transitions and missing evidence do not assert
    either conflict or equality. This function never mutates its argument.
    """
    if workflow.stage not in CHECKED_STAGES:
        return ""
    target = workflow.target
    source_origin, page_origin = _origin(target.source_url), _origin(workflow.url)
    if source_origin is not None and source_origin == page_origin:
        wanted_id, observed_id = _identifier(target.source_url), _identifier(workflow.url)
        if wanted_id and observed_id and wanted_id != observed_id:
            return ("当前页面的岗位编号与本次已选岗位不一致，已停止进入申请和填写。"
                    "请返回本次目标岗位，或明确重新选择投递目标后再继续。")

    wanted, observed = _title(target.job_title), _title(workflow.job_title)
    if not _specific(wanted) or not _specific(observed):
        return ""
    # A short chosen title can legitimately be expanded with a department,
    # level, location or recruitment cycle in the official page.
    if wanted == observed or wanted in observed or observed in wanted:
        return ""
    # Translation without a shared identifier is not enough to prove that two
    # jobs differ. Leave it for the observed-page/target confirmation workflow.
    if bool(re.search(r"[\u4e00-\u9fff]", wanted)) != bool(re.search(r"[\u4e00-\u9fff]", observed)):
        return ""
    return ("当前页面可见的职位名称与本次已选岗位不一致，已停止进入申请和填写。"
            "请核对官方岗位标题；若确实想改投此岗位，请先重新选择目标。")
