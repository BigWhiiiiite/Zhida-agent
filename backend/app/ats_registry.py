"""Trusted ATS policies shared by discovery, planning and execution.

Custom domains require two distinct vendor markers. Shared UI libraries are
not vendor evidence. Policies change selector/wait strategies, not authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field


class SiteRoute(BaseModel):
    adapter: str = "generic"
    label: str = "通用招聘页面"
    matched_by: Literal["host", "dom", "fallback"] = "fallback"
    evidence: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    note: str = "根据当前页面控件处理；未确定的选项会交给你核对。"


COMMON_OPTIONS = (
    '[role="option"]', 'option', '.ant-select-item-option', '.arco-select-option',
    '.el-select-dropdown__item', '.ivu-select-item', '.semi-select-option',
    '[class*="select-option"]', '[class*="dropdown-item"]',
)
COMMON_POPUPS = (
    '[role="listbox"]', '.ant-select-dropdown', '.arco-select-popup',
    '.el-select-dropdown', '.ivu-select-dropdown', '.semi-select-option-list',
)
COMMON_TOOLS = ("observe_page", "map_profile", "fill_text", "select_native",
                "select_scoped_options", "check_choice", "upload_resume", "verify_fields",
                "browse_jobs", "search_jobs", "open_job")


@dataclass(frozen=True)
class ATSPolicy:
    name: str
    label: str
    hosts: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    question_containers: tuple[str, ...] = ()
    label_selectors: tuple[str, ...] = ()
    section_selectors: tuple[str, ...] = ()
    control_selectors: tuple[str, ...] = ()
    option_selectors: tuple[str, ...] = ()
    popup_selectors: tuple[str, ...] = ()
    load_delay_ms: int = 500
    option_wait_ms: int = 900

    def field_profile(self) -> dict:
        return {"name": self.name if self.name != "generic" else "generic-semantic",
                "question_containers": list(self.question_containers),
                "label_selectors": list(self.label_selectors),
                "section_selectors": list(self.section_selectors),
                "control_selectors": list(self.control_selectors),
                "load_delay_ms": self.load_delay_ms}

    @property
    def options(self) -> str:
        return ", ".join((*self.option_selectors, *COMMON_OPTIONS))

    @property
    def popups(self) -> str:
        return ", ".join((*self.popup_selectors, *COMMON_POPUPS))


POLICIES = (
    ATSPolicy(
        "tencent-campus", "腾讯校招", ("join.qq.com",),
        markers=(".atsx-form-item", ".atsx-form-item-label"),
        question_containers=(".atsx-form-item", "[class*='question-item']", "[class*='questionItem']", "[class*='form-item']", "[class*='formItem']"),
        label_selectors=(".atsx-form-item-label", "[class*='question-title']", "[class*='questionTitle']", "[class*='form-label']", "[class*='formLabel']"),
        section_selectors=("[class*='section-title']", "[class*='sectionTitle']"),
        control_selectors=(".atsx-select-selector", ".t-select__wrap"),
        option_selectors=(".atsx-select-item-option", ".t-select-option"),
        popup_selectors=(".atsx-select-dropdown", ".t-select__dropdown"),
        load_delay_ms=900, option_wait_ms=1200,
    ),
    ATSPolicy(
        "moka-campus", "Moka 招聘", ("mokahr.com", "moka.com"),
        markers=(".moka-form-item", ".moka-select", "[data-moka-form]"),
        question_containers=(".application-form-item", ".moka-form-item", "[class*='form-item']", "[class*='formItem']", "[class*='question']", "[data-field]"),
        label_selectors=(".form-label", ".field-label", "[class*='form-label']", "[class*='formLabel']", "[class*='field-label']", "[class*='fieldLabel']", "[class*='question-title']"),
        section_selectors=("[class*='section-title']", "[class*='module-title']"),
        control_selectors=(".moka-select",),
        option_selectors=(".moka-select-option",), popup_selectors=(".moka-select-dropdown",),
        load_delay_ms=1200, option_wait_ms=1600,
    ),
    ATSPolicy(
        "beisen-italent", "北森招聘", ("italent.cn", "beisen.com", "zhiye.com"),
        markers=(".beisen-form-item", ".beisen-select", "[data-beisen-form]"),
        question_containers=(".beisen-form-item", ".ant-form-item", ".el-form-item", "[class*='form-item']", "[class*='formItem']"),
        label_selectors=(".beisen-form-label", ".ant-form-item-label", ".el-form-item__label", "[class*='form-label']", "[class*='field-label']"),
        section_selectors=("[class*='section-title']", "[class*='module-title']"),
        control_selectors=(".beisen-select",),
        option_selectors=(".beisen-select-option",), popup_selectors=(".beisen-select-dropdown",),
        load_delay_ms=1400, option_wait_ms=1800,
    ),
    ATSPolicy("lever", "Lever 招聘", ("lever.co",)),
    ATSPolicy("greenhouse", "Greenhouse 招聘", ("greenhouse.io", "greenhouse.io.cn")),
    ATSPolicy("workday", "Workday 招聘", ("myworkdayjobs.com",)),
    ATSPolicy("generic", "通用招聘页面"),
)
_BY_NAME = {policy.name: policy for policy in POLICIES}


def policy_for(name: str) -> ATSPolicy:
    return _BY_NAME.get(name, _BY_NAME["generic"])


def resolve_site_route(url: str, markers: list[str] | None = None) -> SiteRoute:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    for policy in POLICIES:
        if any(host == suffix or host.endswith("." + suffix) for suffix in policy.hosts):
            return SiteRoute(adapter=policy.name, label=policy.label, matched_by="host",
                             evidence=[f"招聘域名：{host}"], tools=list(COMMON_TOOLS),
                             note="已选择对应站点处理方式，每次填写后会核对网页实际结果。")
    observed = set(markers or [])
    matches = [p for p in POLICIES if len(observed.intersection(p.markers)) >= 2]
    if len(matches) == 1:
        policy = matches[0]
        return SiteRoute(adapter=policy.name, label=policy.label, matched_by="dom",
                         evidence=sorted(observed.intersection(policy.markers)),
                         tools=list(COMMON_TOOLS),
                         note="根据页面组件识别招聘系统；页面变化时会重新识别。")
    return SiteRoute(tools=list(COMMON_TOOLS), evidence=[
        "页面包含多个站点的组件线索，使用通用处理" if matches else "未匹配专用站点规则"
    ])


async def route_for_page(page) -> SiteRoute:
    route = resolve_site_route(page.url)
    if route.matched_by == "host":
        return route
    selectors = list(dict.fromkeys(marker for p in POLICIES for marker in p.markers))
    found = await page.evaluate(
        "selectors => selectors.filter(selector => document.querySelector(selector))", selectors,
    )
    return resolve_site_route(page.url, found)
