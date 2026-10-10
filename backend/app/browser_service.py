from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import mimetypes
import os
import re
import socket
import sys
import time
import unicodedata
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from .application_models import (ApplicationTarget, ApplicationWorkflowState, RegistrationCredentialsRequest,
                                 VerificationCodeRequest, VerificationRequest,
                                 WorkflowAdvanceRequest)
from .ats_field_profiles import field_profile_for_url
from .ats_registry import policy_for, route_for_page
from .ats_controls import scoped_option_entries, visible_popup_ids
from .ats_adapters import (continue_application, create_account, fill_registration_info,
                           fill_verification_code, inspect_application_page,
                           request_verification_code, start_application)
from .browser_models import (ActionResult, BrowserSnapshot, ExecutePlanRequest, ExecutionResult,
                             NativeResumeImportResult, PageField, PreSubmitCheck, RequiredFieldIssue)
from .field_semantics import enrich_fields
from .option_vocabulary import OPTION_ALIASES
from .job_navigation import execute_navigation, choose_candidate, workflow_fingerprint
from .target_identity import target_identity_blocker
from .autohome_fields import refine_autohome_fields
from .autohome_sections import discover_autohome_sections, expand_autohome_section
from .phoenix_sections import discover_phoenix_sections, expand_phoenix_section, inspect_phoenix_sections
from .autohome_attachment import (inspect_autohome_attachment,
                                 confirm_autohome_resume_parse)
from .recognition_diagnostics import inspect_recognition_structure, inspect_component_shapes
from .phoenix_fields import refine_phoenix_fields, CHOICE_STATE_JS, PHOENIX_SELECT_VALUE_JS
from .ant_resume_records import (refine_ant_resume_fields, inspect_ant_resume_records,
                                inspect_ant_resume_rejections)
from .phoenix_calendar import (calendar_ids, calendar_for, calendar_precision,
                               select_calendar_date)
from .phoenix_region import select_region, read_open_region_path, audited_region_path
from .region_facts import region_values_match
from .browser_loading import wait_for_rendered_content
from .external_browser import validate_external_url
from .safari_browser import SafariContext
from .form_field_policy import family_subject, formal_employment_only, is_declaration
from .structured_controls import (classify_controls, preview_calendar, preview_cascade,
                                  select_ant_calendar, select_ant_cascade)
from .execution_safety import ExecutionTargetChanged as _ExecutionTargetChanged, execution_identity_changes
from .form_observation import (annotate_fields, build_report, extraction_block_reason,
                               merge_observed_metadata, refresh_report)


def configured_browser_engine() -> str:
    engine = os.getenv("APP_BROWSER_ENGINE", "auto").strip().lower()
    if engine == "auto":
        return "safari" if sys.platform == "darwin" else "chromium"
    if engine not in {"safari", "chromium"}:
        raise ValueError("APP_BROWSER_ENGINE 只支持 auto、safari 或 chromium")
    return engine


SENSITIVE_FIELD_HINTS = {
    "authorization", "authorized", "sponsorship", "sponsor", "visa", "salary", "compensation",
    "gender", "sex", "race", "ethnicity", "disability", "veteran", "consent", "agree", "agreement",
    "privacy", "terms", "legal", "work permit", "right to work",
    "性别", "薪资", "期望薪资", "签证", "担保", "工作许可", "残障", "退伍", "族裔", "种族", "同意", "隐私", "条款", "法律声明", "承诺", "声明",
    "身份证", "证件号码", "证件号", "护照号码", "护照号", "实名认证", "national id", "id number", "passport number",
    "emergency contact", "next of kin", "guardian", "referee", "recommender",
    "紧急联系人", "紧急联络人", "家属联系人", "监护人", "推荐人", "证明人",
}

MANUAL_CONFIRM_FIELD_HINTS = {
    "是否", "愿意", "调剂", "服从分配", "意向事业群", "感兴趣的事业群", "志愿", "偏好",
    "would you", "are you willing", "preference", "preferred business", "business group", "relocate",
}

OPTION_PLACEHOLDERS = {
    "", "select", "selectone", "choose", "chooseone", "pleasechoose", "请选择", "请选择一项",
    "暂未选择", "未选择", "点击选择", "搜索并选择",
}
UNHELPFUL_FIELD_LABEL = re.compile(
    r"^(?:请输入|请填写|请选择|选择|select|choose|input|field|question)(?:一项|内容|信息|答案|one)?[.\s…:：-]*$",
    re.I,
)
OPAQUE_FIELD_NAME = re.compile(
    r"^(?:field|question|input|select|item|value|answer)?[-_.\[\]0-9a-f]{5,}$",
    re.I,
)
SAFE_RESUME_PARSE_LABEL = re.compile(
    r"^(?:开始)?(?:解析简历|简历解析|智能解析|自动解析|上传并解析|导入简历|从简历导入|"
    r"使用简历填写|一键填充|自动填充)(?:并填充|并导入)?$",
    re.I,
)

# Read only native question ownership, never values/options or arbitrary form
# text. A label wrapping a select must not change identity as choices hydrate.
NATIVE_WRITE_IDENTITIES = r"""selectors => Object.fromEntries(selectors.map(selector => {
  const matches = document.querySelectorAll(selector), el = matches[0];
  if (matches.length !== 1 || !el.matches('input,textarea,select') ||
      el.getAttribute('role') === 'combobox') return [selector, null];
  const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
  const caption = node => {
    if (!node || node === el || el.contains(node)) return '';
    const copy = node.cloneNode(true);
    copy.querySelectorAll('input,textarea,select,button,[role="combobox"],[role="listbox"],[role="option"]')
      .forEach(control => control.remove());
    return clean(copy.textContent);
  };
  const labelledBy = clean(el.getAttribute('aria-labelledby'));
  const container = el.closest('[data-zhida-container]');
  const section = el.closest('fieldset,section,[role="group"]');
  return [selector, {
    tag: el.tagName, type: el.getAttribute('type') || '', name: el.getAttribute('name') || '',
    role: el.getAttribute('role') || '', binding: el.getAttribute('data-bind') || '',
    label: [...(el.labels || [])].map(caption), ariaLabel: el.getAttribute('aria-label') || '',
    labelledBy, labelledText: labelledBy.split(' ').filter(Boolean).map(id => caption(document.getElementById(id))),
    container: container?.getAttribute('data-zhida-container') || '',
    section: section ? [section.tagName, section.id, caption(section.querySelector('legend,h1,h2,h3,h4,[role="heading"]'))] : [],
  }];
}))"""


def _normalized_option(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def _option_alias(value: str) -> int:
    normalized = _option_identity(value)
    for index, group in enumerate(OPTION_ALIASES):
        if normalized in {_option_identity(item) for item in group}:
            return index
    return -1


def _option_identity(value: str) -> str:
    # Unlike a fuzzy field-name key, option identities retain meaningful
    # punctuation: C, C++, C# and Beijing/Shanghai are different choices.
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


def _administrative_core(value: str) -> str:
    text = _option_identity(value)
    if not re.fullmatch(r"[\u4e00-\u9fff]{2,}", text):
        return text
    for suffix in ("特别行政区", "壮族自治区", "回族自治区", "维吾尔自治区", "自治区", "自治州", "地区", "省", "市"):
        if text.endswith(suffix) and len(text) > len(suffix) + 1:
            return text[:-len(suffix)]
    return text


def _option_matches(wanted: str, actual: str) -> bool:
    left, right = _option_identity(wanted), _option_identity(actual)
    if not left or not right or right in OPTION_PLACEHOLDERS:
        return False
    if left == right:
        return True
    left_alias, right_alias = _option_alias(wanted), _option_alias(actual)
    if left_alias >= 0 or right_alias >= 0:
        return left_alias >= 0 and left_alias == right_alias
    # Only administrative suffixes have a deterministic equivalence. A unique
    # substring is not enough: 1 != 10年, 北京 != 北京/上海, 计算机 != 非计算机专业.
    return _administrative_core(wanted) == _administrative_core(actual)


def _best_option(wanted: str, candidates: list[str]) -> str:
    exact = [item for item in candidates if _option_identity(item) == _option_identity(wanted)]
    if exact:
        return exact[0] if len(exact) == 1 else ""
    matched = [item for item in candidates if _option_matches(wanted, item)]
    return matched[0] if len(matched) == 1 else ""


def _split_values(value: str | bool) -> list[str]:
    return [part.strip() for part in re.split(r"[,，\n]", str(value)) if part.strip()]


def _selected_values_match(expected: list[str], actual: str) -> bool:
    actual_values = _split_values(actual)
    if not expected or len(expected) != len(actual_values):
        return False
    remaining = list(actual_values)
    for wanted in expected:
        matches = [index for index, item in enumerate(remaining) if _option_matches(wanted, item)]
        if len(matches) != 1:
            return False
        remaining.pop(matches[0])
    return not remaining


def _field_identity(field: PageField) -> str:
    return _normalized_option(" ".join(filter(None, (
        field.section, field.group_label, field.label, field.context, field.name, field.field_type,
    ))))


def _clean_metadata(value: object, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _useful_field_label(value: str) -> bool:
    normalized = value.strip()
    return bool(
        normalized
        and not UNHELPFUL_FIELD_LABEL.fullmatch(normalized)
        and not re.fullmatch(r"[+()\-./\s\d．、:：]+", normalized)
        and len(_normalized_option(normalized)) > 1
    )


def _context_label(value: str) -> str:
    text = _clean_metadata(value, 600)
    if not text:
        return ""
    text = re.sub(
        r"(?:请输入|请填写|请选择|选择一项|select one|please choose)[.\s…:：-]*$", "", text,
        flags=re.I,
    ).strip()
    if len(text) <= 180:
        return text
    parts = [item.strip() for item in re.split(r"[。！？!?；;]", text) if item.strip()]
    return next((item for item in parts if 2 <= len(item) <= 180), text[:177] + "…")


def _finalize_field_metadata(items: list[dict[str, object]]) -> None:
    """Guarantee a user-facing title while retaining the raw DOM name for execution."""
    for index, item in enumerate(items, start=1):
        item["ordinal"] = index
        item["context"] = _clean_metadata(item.get("context"), 600)
        item["help_text"] = _clean_metadata(item.get("help_text"), 500)
        item["nearby_labels"] = list(dict.fromkeys(
            _clean_metadata(value, 300) for value in (item.get("nearby_labels") or [])
            if _clean_metadata(value, 300)
        ))[:8]
        item["section_path"] = list(dict.fromkeys(
            _clean_metadata(value, 200) for value in (item.get("section_path") or [])
            if _clean_metadata(value, 200)
        ))[:8]
        item["placeholder"] = _clean_metadata(item.get("placeholder"), 300)
        label = _clean_metadata(item.get("question_text") or item.get("label"), 500)
        source = _clean_metadata(item.get("label_source"), 40) or "unknown"
        if source == "placeholder":
            label = re.sub(r"^(?:请输入|请填写|请选择|选择)\s*", "", label, flags=re.I).strip(" .…:：-")
        if not _useful_field_label(label):
            candidates = (
                (_clean_metadata(item.get("group_label"), 500), "nearby"),
                *[(value, "nearby") for value in item["nearby_labels"]],
                (_context_label(str(item.get("context") or "")), "context"),
                (re.sub(r"^(?:请输入|请填写|请选择|选择)\s*", "",
                        _clean_metadata(item.get("placeholder"), 300), flags=re.I).strip(" .…:：-"),
                 "placeholder"),
            )
            label, source = next(
                ((candidate, candidate_source) for candidate, candidate_source in candidates
                 if _useful_field_label(candidate)),
                ("", "unknown"),
            )
        name = _clean_metadata(item.get("name"), 500)
        if not label and name and not OPAQUE_FIELD_NAME.fullmatch(name):
            label = re.sub(r"[_-]+", " ", name).strip()
            source = "name"
        if not label:
            label = f"未识别字段 {index}"
            source = "generated"
        item["label"] = label
        item["question_text"] = label
        item["label_source"] = source
        default_confidence = {
            "explicit": .98, "aria-labelledby": .96, "nearby": .6, "aria": .86,
            "container-owned": .95,
            "attribute": .78, "context": .72, "placeholder": .62, "name": .45,
            "generated": .1, "unknown": .1,
        }.get(source, .5)
        raw_confidence = item.get("recognition_confidence")
        try:
            confidence = float(raw_confidence) if raw_confidence not in (None, "") else default_confidence
        except (TypeError, ValueError):
            confidence = default_confidence
        # Familiar wording does not prove ownership of an adjacent control.
        if source not in {"explicit", "aria-labelledby", "container-owned", "aria"}:
            confidence = min(confidence, default_confidence)
        item["recognition_confidence"] = max(0, min(1, confidence))


def _choice_only_text(value: str, options: list[str]) -> bool:
    """Return true when text is only one or more option captions (for example 是/否)."""
    remaining = _normalized_option(value)
    option_tokens = sorted({_normalized_option(option) for option in options if option}, key=len, reverse=True)
    if not remaining or not option_tokens:
        return False
    previous = None
    while previous != remaining:
        previous = remaining
        for token in option_tokens:
            remaining = remaining.replace(token, "")
    return not remaining


def _strip_choice_suffix(value: str, options: list[str]) -> str:
    """Remove rendered option captions from the end of an ancestor's visible question text."""
    result = _clean_metadata(value, 500)
    tokens = sorted({option.strip() for option in options if option.strip()}, key=len, reverse=True)
    changed = True
    while result and changed:
        changed = False
        for token in tokens:
            updated = re.sub(
                rf"(?:^|[\s/、，,;；|·—-]){re.escape(token)}[\s/、，,;；|·—-]*$", "", result,
                flags=re.I,
            ).strip()
            if updated != result:
                result, changed = updated, True
                break
    return result


def _finalize_choice_metadata(items: list[dict[str, object]]) -> None:
    """Unify choice controls under one meaningful question and fail closed without a prompt."""
    groups: dict[str, list[dict[str, object]]] = {}
    for item in items:
        if item.get("field_type") not in {"radio", "checkbox"}:
            continue
        key = _clean_metadata(item.get("control_group_key"), 500)
        if not key:
            name = _clean_metadata(item.get("name"), 500)
            key = f"{item.get('field_type')}:{item.get('container_key', '')}:name:{name}" if name else ""
        if key:
            groups.setdefault(key, []).append(item)

    for group in groups.values():
        options = list(dict.fromkeys(
            _clean_metadata(option, 300)
            for item in group
            for option in [*(item.get("options") or []), item.get("option_label")]
            if _clean_metadata(option, 300)
        ))
        if not options:
            options = list(dict.fromkeys(
                _clean_metadata(item.get("option_value"), 300) for item in group
                if _clean_metadata(item.get("option_value"), 300)
            ))
        candidates = [
            value
            for item in group
            for value in (item.get("group_label"), item.get("question_text"), item.get("context"))
        ]
        prompt = ""
        if len(group) == 1 and group[0].get('field_type') == 'checkbox':
            item = group[0]
            question = _clean_metadata(item.get('question_text'), 500)
            exact_owned = (item.get('label_source') in {'explicit', 'aria-labelledby', 'container-owned', 'aria'}
                           and any(evidence.get('owned') and
                                   _clean_metadata(evidence.get('text'), 500).casefold() == question.casefold()
                                   for evidence in item.get('question_candidates', [])))
            # A standalone attestation's statement is both its caption and its
            # single option. Preserve only an exact, independently owned local
            # statement; execution still denies every declaration via policy.
            if question and exact_owned and is_declaration(PageField.model_validate(item)):
                prompt = question
        for candidate in candidates:
            if prompt:
                break
            cleaned = _strip_choice_suffix(str(candidate or ""), options)
            if (_useful_field_label(cleaned) and not _choice_only_text(cleaned, options)
                    and len(cleaned) > 1):
                prompt = cleaned
                break
        if not prompt:
            ordinal = min((int(item.get("ordinal") or 0) for item in group), default=0)
            yes_no = {_normalized_option(option) for option in options}.issubset(
                {_normalized_option(option) for option in ("是", "否", "yes", "no", "true", "false")}
            )
            kind = ("是/否问题" if yes_no and len(options) >= 2 else
                    "复选题" if group[0].get("field_type") == "checkbox" else "单选题")
            prompt = f"未识别的{kind}（网页第 {ordinal} 个控件附近）"
        for item in group:
            previous_question = str(item.get("question_text") or "")
            item["group_label"] = prompt
            item["question_text"] = prompt
            item["label"] = prompt
            if not prompt.startswith("未识别的"):
                if not (item.get("label_source") in {"explicit", "aria-labelledby", "container-owned", "aria"}
                        and previous_question == prompt):
                    item["label_source"] = "nearby"
                if item["label_source"] == "nearby":
                    item["recognition_confidence"] = min(float(item.get("recognition_confidence") or .6), .6)
            item["options"] = options


def _meaningful_value(field: PageField) -> str:
    value = field.current_value.strip()
    return "" if _normalized_option(value) in OPTION_PLACEHOLDERS else value


def _changed_field_count(before: BrowserSnapshot, after: BrowserSnapshot) -> int:
    old = {_field_identity(field): _meaningful_value(field) for field in before.fields
           if field.field_type not in {"file", "section-button"}}
    return sum(
        bool(value) and value != old.get(_field_identity(field), "")
        for field in after.fields
        if field.field_type not in {"file", "section-button"}
        for value in [_meaningful_value(field)]
    )


class NavigationNoProgressError(ValueError):
    """An attempted safe navigation stopped; this is not a lost browser session."""

    def __init__(self, message: str, workflow: ApplicationWorkflowState):
        super().__init__(message)
        self.workflow = workflow


class BrowserDemoService:
    def __init__(self) -> None:
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.session_id: str | None = None
        self.target = ApplicationTarget()
        self._stalled_navigation: set[str] = set()
        self._option_probe_shapes: dict[str, list[dict]] = {}
        self._keep_safari_window = False

    @staticmethod
    def profile_directory(user_id: str) -> Path:
        default_root = Path(__file__).resolve().parents[1] / "data" / "browser_profiles"
        root = Path(os.getenv("APP_BROWSER_PROFILE_DIR", str(default_root))).expanduser().resolve()
        profile_key = hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:32]
        return root / profile_key

    @staticmethod
    def _enabled(name: str, default: bool) -> bool:
        fallback = "true" if default else "false"
        return os.getenv(name, fallback).strip().lower() in {"1", "true", "yes"}

    @staticmethod
    def _validate_url(url: str) -> str:
        parsed = urlparse(url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("请输入完整的 http:// 或 https:// 招聘页面地址")
        if parsed.username or parsed.password:
            raise ValueError("URL 中不能包含用户名或密码")
        host = parsed.hostname.lower().rstrip(".")
        if host in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("当前地址是本机页面，不是招聘官网。请在职达打开的 Safari 窗口中切回招聘网站标签页，再连接")
        # Apply the same public-domain syntax/port policy as ordinary opening.
        # DNS is still checked for automation; opening a normal window is not
        # permission to read/fill a redirected local or private destination.
        value = validate_external_url(url)
        try:
            addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80),
                                           type=socket.SOCK_STREAM)
            if not addresses:
                raise ValueError(f"招聘域名 {host} 没有返回可验证的地址，未连接自动填写")
            for item in addresses:
                ip = ipaddress.ip_address(item[4][0])
                if not ip.is_global or ip.is_multicast:
                    if ip.version == 4 and ip in ipaddress.ip_network("198.18.0.0/15"):
                        reason = "测试网段地址，可能是代理的 Fake-IP 占位地址"
                        action = "请检查代理的 DNS/Fake-IP 设置，对该招聘域名使用真实 DNS 后重试"
                    else:
                        reason = "本机、内网或保留地址"
                        action = "请核对 Safari 地址栏是否仍为招聘官网；若是，请把此提示和网址发给我排查"
                    raise ValueError(f"招聘域名 {host} 解析到了{reason}。为保护本机数据，未连接自动填写。{action}")
        except socket.gaierror as exc:
            raise ValueError(f"职达暂时无法解析招聘域名 {host}。浏览器能打开不代表后端 DNS 一定可用；请检查网络或代理后重试") from exc
        return value

    async def start(self, url: str, user_id: str = "local",
                    target: ApplicationTarget | None = None, safari_page=None) -> BrowserSnapshot:
        url = self._validate_url(url)
        await self.close()
        self.target = target.model_copy(deep=True) if target else ApplicationTarget()
        engine = configured_browser_engine()
        if engine == "safari":
            phase = "launch"
            try:
                if sys.platform != "darwin":
                    raise RuntimeError("Safari 模式仅支持 macOS，请设置 APP_BROWSER_ENGINE=chromium")
                if self._enabled("APP_BROWSER_HEADLESS", False):
                    raise ValueError("Safari 普通窗口不支持无界面模式，请关闭 APP_BROWSER_HEADLESS")
                self.context = SafariContext()
                if safari_page is not None:
                    self.context = safari_page.context
                    self.page = safari_page
                    self._keep_safari_window = True
                    await self.page.ensure_available()
                else:
                    self.page = await self.context.new_page(url)
                self.session_id = str(uuid4())
                phase = "current_url_validation"
                try:
                    self._validate_url(await self.page.read_url())
                except ValueError as exc:
                    raise ValueError("检查职达绑定的 Safari 招聘页时失败：" + str(exc)) from exc
                self.page.url_validator = self._validate_url
                phase = "automation_connection"
                await self.page.wait_for_load_state("domcontentloaded", timeout=60000)
                # A reused window may now be at login, job detail or an application.
                # Observe its real URL, never navigate it back to the initial homepage.
                phase = "current_url_validation"
                try:
                    self._validate_url(self.page.url)
                except ValueError as exc:
                    raise ValueError("检查当前 Safari 标签页时失败：" + str(exc)) from exc
                phase = "rendered_content"
                await wait_for_rendered_content(self.page)
                phase = "snapshot"
                return await self.snapshot()
            except Exception as exc:
                retained = bool(self.context and self.context.pages)
                if retained and not self._keep_safari_window:
                    # Connection errors must not erase the user's opened site.
                    # Drop only our handles; no active filling session remains.
                    self.context.release_windows()
                await self.close()
                logging.getLogger(__name__).warning(
                    "browser_start_failed engine=safari phase=%s error_type=%s window_retained=%s",
                    phase, type(exc).__name__, retained,
                )
                # No silent fallback that unexpectedly opens Chrome.
                if retained:
                    raise RuntimeError("Safari 招聘窗口已保留，但自动识别未连接；没有执行填写。"
                                       "你可以先手动浏览，或使用‘直接打开招聘网站’。连接原因：" + str(exc)) from exc
                raise
        self.playwright = await async_playwright().start()
        try:
            channel = os.getenv("APP_BROWSER_CHANNEL", "chrome")
            headless = self._enabled("APP_BROWSER_HEADLESS", False)
            if self._enabled("APP_BROWSER_PERSISTENT", True):
                profile_dir = self.profile_directory(user_id)
                profile_dir.mkdir(parents=True, exist_ok=True)
                try:
                    profile_dir.chmod(0o700)
                except OSError:
                    pass
                self.context = await self.playwright.chromium.launch_persistent_context(
                    str(profile_dir), channel=channel, headless=headless,
                    viewport={"width": 1280, "height": 850}, locale="zh-CN",
                )
                self.browser = self.context.browser
            else:
                self.browser = await self.playwright.chromium.launch(channel=channel, headless=headless)
                self.context = await self.browser.new_context(
                    viewport={"width": 1280, "height": 850}, locale="zh-CN",
                )
        except Exception as exc:
            await self.close()
            raise RuntimeError("无法启动浏览器，请确认已安装 Chrome，或运行 playwright install chromium 并设置 APP_BROWSER_CHANNEL=chromium") from exc
        pages = [page for page in self.context.pages if not page.is_closed()]
        self.page = pages[0] if pages else await self.context.new_page()
        self.session_id = str(uuid4())
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            await self.page.wait_for_timeout(int(field_profile_for_url(url)["load_delay_ms"]))
            await wait_for_rendered_content(self.page)
            return await self.snapshot()
        except Exception:
            await self.close()
            raise

    def _require(self, session_id: str) -> Page:
        if not self.page or not self.session_id or session_id != self.session_id:
            raise LookupError("浏览器会话不存在或已经结束")
        if self.page.is_closed() and self.context:
            open_pages = [page for page in self.context.pages if not page.is_closed()]
            if open_pages:
                self.page = open_pages[-1]
            else:
                raise LookupError("浏览器页面已关闭")
        return self.page

    async def workflow_state(self, session_id: str) -> ApplicationWorkflowState:
        page = self._require(session_id)
        if isinstance(self.context, SafariContext):
            await page.refresh()
        state = await inspect_application_page(page, session_id, self.target)
        conflict = target_identity_blocker(state)
        if conflict:
            state.navigation_blocker = conflict
            return state
        if any(key.startswith(workflow_fingerprint(state) + "|") for key in self._stalled_navigation):
            state.navigation_blocker = "上一次导航未观察到页面进展，已停止重复操作。请更换真实候选或手动导航后重新识别。"
        return state

    async def advance_workflow(self, session_id: str,
                               request: WorkflowAdvanceRequest) -> ApplicationWorkflowState:
        page = self._require(session_id)
        before = await self.workflow_state(session_id)
        if before.navigation_blocker and request.intent not in {"refresh", "browse_jobs", "search_jobs", "open_job"}:
            raise ValueError(before.navigation_blocker)
        candidate = None
        if request.intent in {"browse_jobs", "search_jobs", "open_job"}:
            if before.stage not in {"homepage", "job_list", "unknown"}:
                raise ValueError("当前不是招聘导航页面，禁止将申请表控件用作搜索或选岗")
            candidate = choose_candidate(before.navigation_candidates, request.intent,
                                         self.target, request.candidate_id)
            if candidate.url:
                self._validate_url(candidate.url)
        operation = f"{workflow_fingerprint(before)}|{request.intent}|{candidate.id if candidate else ''}"
        if request.intent != "refresh" and operation in self._stalled_navigation:
            raise ValueError("已阻止重复的无进展操作；请重新选择入口或在招聘网页中导航后再识别")
        if candidate:
            await execute_navigation(page, request.intent, self.target, candidate.id)
        elif request.intent == "start_application":
            if before.stage != "job_detail":
                raise ValueError("只能从明确识别的岗位详情页进入申请，不能在首页或预览页点击投递")
            await start_application(page, self.target)
        elif request.intent == "create_account":
            await create_account(page)
        elif request.intent == "continue_application":
            if before.stage not in {"profile_form", "application_form"}:
                raise ValueError("当前不是可继续的申请表页面，不能自动翻页或提交")
            check = await self.pre_submit_check(session_id)
            blockers = [
                f"{len(check.required_missing)} 个必填项未填",
                f"{len(check.validation_errors)} 个页面错误",
                f"{len(check.human_challenges)} 个人工验证",
            ]
            if not check.ready:
                detail = "、".join(item for item, blocked in zip(
                    blockers, (check.required_missing, check.validation_errors, check.human_challenges)
                ) if blocked)
                raise ValueError(f"当前页还不能继续：{detail or '请先完成表单检查'}")
            await continue_application(page)
        if self.context:
            pages = [candidate for candidate in self.context.pages if not candidate.is_closed()]
            if pages:
                self.page = page = pages[-1]
        after = await self.workflow_state(session_id)
        # Search/navigation results may arrive after the control's event. Wait
        # briefly for observable state, without clicking/typing a second time.
        # Input text alone is not evidence of progress; keep the stalled guard.
        if candidate:
            deadline = time.monotonic() + 4
            while workflow_fingerprint(before) == workflow_fingerprint(after) and time.monotonic() < deadline:
                await asyncio.sleep(0.35)
                if self.context:
                    pages = [item for item in self.context.pages if not item.is_closed()]
                    if pages:
                        self.page = pages[-1]
                after = await self.workflow_state(session_id)
        if request.intent != "refresh" and workflow_fingerprint(before) == workflow_fingerprint(after):
            self._stalled_navigation.add(operation)
            message = "已尝试操作，但未观察到网址、阶段、岗位或候选列表变化；未确认流程前进，已停止自动重复"
            after.navigation_blocker = message
            raise NavigationNoProgressError(message, after)
        return after

    async def fill_registration(self, session_id: str,
                                request: RegistrationCredentialsRequest) -> ApplicationWorkflowState:
        page = self._require(session_id)
        password = request.password.get_secret_value() if request.password else ""
        try:
            await fill_registration_info(page, request.email.strip(), request.phone.strip(), password)
        finally:
            password = ""
        return await inspect_application_page(page, session_id, self.target)

    async def enter_verification(self, session_id: str,
                                 request: VerificationCodeRequest) -> ApplicationWorkflowState:
        page = self._require(session_id)
        code = request.code.get_secret_value()
        try:
            await fill_verification_code(page, code, request.submit)
        finally:
            code = ""
        if self.context:
            pages = [candidate for candidate in self.context.pages if not candidate.is_closed()]
            if pages:
                self.page = page = pages[-1]
        return await inspect_application_page(page, session_id, self.target)

    async def request_code(self, session_id: str, request: VerificationRequest,
                           phone: str, email: str) -> ApplicationWorkflowState:
        page = self._require(session_id)
        value = request.value.strip() or (phone if request.channel == "phone" else email)
        await request_verification_code(page, request.channel, value)
        return await inspect_application_page(page, session_id, self.target)

    async def snapshot(self, *, probe_options: bool = False) -> BrowserSnapshot:
        if not self.page or not self.session_id:
            raise LookupError("浏览器会话尚未启动")
        document = await self.page.evaluate_handle('() => document')
        try:
            result = await self._snapshot_bound_document(probe_options=probe_options)
            if not await document.evaluate('original => original === document'):
                raise ValueError('读取控件期间网页已重新加载；旧表单信息已丢弃，请重新读取当前页面')
            return result
        finally:
            try:
                await document.dispose()
            except Exception:
                pass

    async def _snapshot_bound_document(self, *, probe_options: bool = False) -> BrowserSnapshot:
        """Capture questions without UI actions unless a preview is explicit.

        Initial connection, synchronization and execution readbacks must not
        expand unrelated dropdowns (including account/declaration widgets).
        Missing options remain explicit; bounded observation tools or the
        executor inspect a proven field when it is actually needed.
        """
        if not self.page or not self.session_id:
            raise LookupError("浏览器会话尚未启动")
        if isinstance(self.context, SafariContext):
            await self.page.refresh()
        document_url = self.page.url
        site_route = await route_for_page(self.page)
        await self._assert_snapshot_document(document_url, phase="site_routing")
        policy = policy_for(site_route.adapter)
        recognition_profile = policy.field_profile()
        captured = await self.page.evaluate("""
        profile => {
          const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
          const captionCopy = node => {
            const clone=node.cloneNode(true);
            const originals=[...node.querySelectorAll('*')],copies=[...clone.querySelectorAll('*')];
            originals.forEach((original,index)=>{
              const style=getComputedStyle(original);
              if (original.closest('[hidden],[aria-hidden="true"]') || style.display==='none' ||
                  ['hidden','collapse'].includes(style.visibility)) copies[index]?.remove();
            });
            clone.querySelectorAll('input, select, textarea, option, button, script, style, svg, [hidden], [aria-hidden="true"], [role="option"], [role="radio"], [role="checkbox"]').forEach(item=>item.remove());
            return clone;
          };
          const captionCache=new WeakMap();
          const labelText = node => {
            if (!node) return '';
            if (captionCache.has(node)) return captionCache.get(node);
            const clone=captionCopy(node);
            const result=clean(clone.innerText || clone.textContent);
            captionCache.set(node,result); return result;
          };
          const normalizeFieldText = value => clean(value).replace(/^[＊*]+\s*|[＊*]+\s*$/g, '').trim();
          const meaningfulFieldText = value => {
            const text = normalizeFieldText(value);
            return Boolean(text && text.length > 1 && !/^[+()\-.、。．\s\d/:：]+$/.test(text));
          };
          const normalizedToken = value => normalizeFieldText(value).toLowerCase()
            .replace(/[^a-z0-9\u4e00-\u9fff]/g, '');
          const fieldTextScore = (value, source = 'unknown', optionTexts = []) => {
            const text = normalizeFieldText(value);
            if (!meaningfulFieldText(text)) return -1000;
            const token = normalizedToken(text);
            const optionTokens = optionTexts.map(normalizedToken).filter(Boolean);
            if (optionTokens.includes(token)) return -90;
            let score = ({explicit: 42, 'aria-labelledby': 40, 'container-owned': 37, nearby: 34, preceding: 31,
              aria: 23, attribute: 18, placeholder: 12, name: 5}[source] || 0);
            if (text.length <= 16) score += 12;
            else if (text.length <= 40) score += 9;
            else if (text.length <= 100) score += 4;
            else if (text.length > 220) score -= 22;
            if (/[？?:：]$/.test(text)) score += 5;
            if (/(姓名|邮箱|手机|电话|性别|国家|城市|地区|地点|学校|院校|学历|学位|学院|专业|毕业|培养|学制|导师|实验室|经历|项目|技能|语言|意向|期望|是否|接受|职位|公司|name|email|phone|gender|country|city|location|school|degree|major|experience|project|skill|language|preference)/i.test(text)) score += 13;
            if (/^(请输入|请填写|请选择|选择|select|choose)(一项|内容|信息|答案|one)?[.\s…:：-]*$/i.test(text)) score -= 80;
            if (/^(?:field|question|input|select|item|value|answer)?(?:[-_.0-9a-f]|\\[|\\]){5,}$/i.test(text)) score -= 45;
            if (/^(?:是|否|男|女|yes|no|true|false|北京|上海|请选择)$/i.test(text)) score -= 35;
            return score;
          };
          const bestFieldText = (candidates, optionTexts = []) => {
            const ranked = candidates.map(item => ({...item, text: normalizeFieldText(item.text),
              score: fieldTextScore(item.text, item.source, optionTexts)}))
              .filter(item => item.text).sort((left, right) => right.score - left.score);
            // A label linked to this exact control outranks a familiar-looking
            // neighbour. Keyword scores must not turn an adjacent 姓名 into
            // the title of an explicitly labelled 称呼 field.
            const owned = ranked.filter(item => ['explicit', 'aria-labelledby'].includes(item.source)
              && item.score > -20);
            if (owned.length) return owned[0];
            const containerOwned = ranked.find(item => item.source === 'container-owned' && item.score > -20);
            if (containerOwned) return containerOwned;
            return ranked[0] && ranked[0].score > -20 ? ranked[0] : {text: '', source: 'unknown', score: -1000};
          };
          const questionEvidence = candidates => {
            const evidence = candidates.map(item => ({text:normalizeFieldText(item.text).slice(0,500),
              source:item.source, owned:['explicit','aria-labelledby','container-owned','aria'].includes(item.source)}))
              .filter(item => meaningfulFieldText(item.text));
            const distinct=[...new Map(evidence.map(item => [item.source+'|'+item.text,item])).values()];
            return [...distinct.filter(item => item.owned).slice(0,8),
                    ...distinct.filter(item => !item.owned).slice(0,4)];
          };
          const placeholder = value => /^(select|select one|choose|choose one|please choose|请选择|请选择一项|暂未选择|未选择|点击选择|搜索并选择)[.\\s…]*$/i.test(clean(value));
          const customWrapperSelector = [
            ...(profile?.control_selectors || []),
            '.ant-select-selector', '.ant-select-selection', '.ant-calendar-picker', '.ant-picker', '.ant-cascader-picker', '.ant-cascader', '.arco-select-view', '.el-select__wrapper', '.ivu-select-selection',
            '.semi-select', '.t-select__wrap', '[class*="select-selector"]',
            '[class*="select__selector"]', '[class*="select-view"]',
            '[class*="cascader-picker"]', '[class*="picker-input"]'
          ].join(', ');
          const customSelector = '[role="combobox"], [aria-haspopup="listbox"], ' + customWrapperSelector;
          const radioSelector = ['input[type="radio"]', '[role="radio"]', ...(profile?.radio_selectors || [])].join(', ');
          const controlSelector = 'input, select, textarea, [role="checkbox"], [role="combobox"], [aria-haspopup="listbox"], ' + radioSelector;
          const renderedCache = new WeakMap();
          const rendered = el => {
            if (!el || el.closest('[hidden], [aria-hidden="true"]')) return false;
            if (renderedCache.has(el)) return renderedCache.get(el);
            for (let node = el; node && node !== document; node = node.parentElement) {
              const style = getComputedStyle(node);
              if (style.display === 'none' || ['hidden', 'collapse'].includes(style.visibility)) {
                renderedCache.set(el, false); return false;
              }
            }
            const result = el.getClientRects().length > 0;
            renderedCache.set(el, result); return result;
          };
          // Upload widgets intentionally hide their native file input. It still
          // belongs to the visible local widget and must count when proving a
          // question owner; otherwise its title competes with upload tips.
          const renderedControl = el => rendered(canonical(el)) ||
            (el.type === 'file' && !el.closest('[hidden],[aria-hidden="true"]') && rendered(el.parentElement));
          const captionSelector = 'label,legend,[class*="label" i],[class*="caption" i],[class*="title" i]';
          const helperSelector = 'small,.help,.hint,.tip,.description,[class*="help" i],[class*="error" i],[class*="tip" i]';
          const ownedCaptionCache=new WeakMap();
          const ownedCaptionText = node => {
            if (!node) return '';
            if (ownedCaptionCache.has(node)) return ownedCaptionCache.get(node);
            const clone=captionCopy(node);
            clone.querySelectorAll(helperSelector+',header,nav,[role="navigation"]').forEach(item=>item.remove());
            const result=clean(clone.innerText || clone.textContent);
            ownedCaptionCache.set(node,result); return result;
          };
          const questionTitleSelector = 'legend,.application-label,.question-label,[data-qa="question-label"],.ant-form-item-label,.el-form-item__label,.arco-form-label-item,[class*="question-title"],[class*="questionTitle"],[class*="form-title"],.resume-form-title';
          // Deeply wrapped controls often have no label[for]. Find the smallest
          // owner with exactly one logical control and its own distinct caption.
          // A placeholder, dropdown option, sibling question or section heading
          // is never promoted to a verified question merely by proximity.
          const ownedQuestion = el => {
            const excluded = /^(?:请输入|请填写|请选择|选择|select|choose|男|女|是|否|至今|上传文件|个人信息|教育经历|实习经历|项目经历|语言能力|证书|技能|获奖情况|简历附件)[.\\s…:：*＊]*$/i;
            let node = el.parentElement;
            for (let depth = 0; node && depth < 14 && node !== document.body; depth += 1, node = node.parentElement) {
              if (node.matches('header, nav, [role="navigation"], [role="search"]')) return null;
              const logical = [...new Set([...node.querySelectorAll(controlSelector + ', ' + customSelector)]
                .filter(item => item.type !== 'hidden' && !item.disabled && renderedControl(item))
                .map(canonical))];
              if (logical.length !== 1 || logical[0] !== canonical(el)) continue;
              const captions = [...node.children].filter(child => child !== el && !child.contains(el) &&
                !child.matches('header,nav,[role="navigation"],'+helperSelector) &&
                !child.querySelector(controlSelector + ', ' + customSelector) && rendered(child) &&
                (child.matches(captionSelector) || child.querySelector(captionSelector)))
                .map(child => ({node: child, text: normalizeFieldText(ownedCaptionText(child))}))
                .filter(item => meaningfulFieldText(item.text) && item.text.length <= 220 && !excluded.test(item.text));
              const distinct = [...new Map(captions.map(item => [normalizedToken(item.text), item])).values()];
              if (distinct.length !== 1) continue;
              const caption = distinct[0];
              if (!caption.node.matches('label, legend, [class*="label" i], [class*="caption" i], [class*="title" i]') &&
                  !caption.node.querySelector('label, legend, [class*="label" i], [class*="caption" i], [class*="title" i]')) continue;
              return {node, caption: caption.text, captionNode: caption.node};
            }
            return null;
          };
          // Ant option labels contain "form-item" / "checkbox-group-item".
          // They own an answer, not the entire question. Substring selectors
          // must never stop at such a label or split one question into options.
          const choiceOptionWrapper = node => node?.matches('input,[role="radio"],[role="checkbox"],label,.ant-radio,.ant-checkbox,.ant-radio-wrapper,.ant-checkbox-wrapper,[class*="checkbox-group-item"],[class*="radio-group-item"]');
          const fieldContainerSelector = [
            ...(profile?.question_containers || []),
            '.application-question', '.application-additional', 'fieldset', '[role="radiogroup"]', '[role="group"]',
            '.form-field', '.field', '.ant-form-item', '.arco-form-item', '.el-form-item',
            '.form-group', '.atsx-form-item', '[class*="form-item"]', '[class*="formItem"]',
            '[class*="form_item"]', '[class*="question-item"]', '[class*="questionItem"]',
            '[data-qa*="question"]', '[data-field]'
          ].join(', ');
          const fieldContainer = el => {
            const phoenix = el.closest('.form-item.form-item--phoenix');
            if (phoenix) return phoenix;
            for (let node=el; node && node!==document.body; node=node.parentElement) {
              if (!choiceOptionWrapper(node) && node.matches(fieldContainerSelector)) return node;
            }
            return null;
          };
          const semanticContainer = el => {
            const owned = ownedQuestion(el);
            if (owned) return owned.node;
            const known = fieldContainer(el);
            if (known) return known;
            let node = el.parentElement;
            for (let depth = 0; node && depth < 8 && node !== document.body; depth += 1, node = node.parentElement) {
              const text = labelText(node);
              const controls = node.querySelectorAll(controlSelector).length;
              if (text && text.length <= 500 && controls <= 4) return node;
            }
            return null;
          };
          const entityContainerFor = el => {
            const named = el.closest([
              '.education-item', '.educationItem', '.education-experience-item', '.academic-item',
              '.experience-item', '.experienceItem', '.project-item', '.projectItem',
              '[data-qa*="education"]', '[data-testid*="education"]',
              '[class*="education-item"]', '[class*="educationItem"]',
              '[class*="academic-item"]', '[class*="academicItem"]'
            ].join(', '));
            if (named && named.querySelectorAll(controlSelector).length > 1) return named;
            const semantic = semanticContainer(el);
            let node = semantic?.parentElement || el.parentElement;
            for (let depth = 0; node && depth < 7 && node !== document.body; depth += 1, node = node.parentElement) {
              const text = labelText(node);
              const controls = node.querySelectorAll(controlSelector).length;
              const education = /(教育|学历|本科|硕士|博士|院校|学校|专业|education|academic|university|school|degree|major)/i.test(text);
              if (education && controls >= 2 && controls <= 14 && text.length <= 1800) return node;
            }
            return semantic;
          };
          const select2Input = el => {
            const wrapper = el.closest('.select2-container');
            const native = wrapper?.previousElementSibling;
            return native?.matches('select.select2-hidden-accessible') ? native : null;
          };
          const inputFor = el => select2Input(el) || (el.matches('input, select, textarea') ? el : el.querySelector('input, select, textarea'));
          const labelledBy = el => clean((el.getAttribute('aria-labelledby') || '').split(/\\s+/)
            .map(id => document.getElementById(id)?.innerText || '').join(' '));
          const nearbyLabel = el => {
            const container = semanticContainer(el);
            if (!container) return '';
            const selector = [
              ...(profile?.label_selectors || []),
              'legend', '.application-label', '.question-label', '[data-qa="question-label"]',
              '.ant-form-item-label', '.arco-form-label-item', '.el-form-item__label',
              '[class*="form-label"]', '[class*="field-label"]', '[class*="item-label"]',
              '[class*="formLabel"]', '[class*="fieldLabel"]', '[class*="questionTitle"]'
            ].join(', ');
            return [...container.querySelectorAll(selector)]
              .map(item => labelText(item))
              .find(value => value && value.length <= 300) || '';
          };
          const precedingLabel = el => {
            let node = el;
            for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
              let sibling = node.previousElementSibling;
              while (sibling) {
                const text = labelText(sibling);
                const siblingControls = sibling.querySelectorAll(controlSelector).length;
                if (!siblingControls && text && text.length <= 300) return text;
                sibling = sibling.previousElementSibling;
              }
            }
            return '';
          };
          const nearbyLabelCandidates = el => {
            const values = [];
            const push = value => {
              const text = normalizeFieldText(value);
              if (meaningfulFieldText(text) && text.length <= 300 && !values.includes(text)) values.push(text);
            };
            const labelLike = [
              ...(profile?.label_selectors || []),
              'legend', 'label', '[role="heading"]', '.application-label', '.question-label',
              '[data-qa="question-label"]', '.ant-form-item-label', '.arco-form-label-item',
              '.el-form-item__label', '[class*="form-label"]', '[class*="field-label"]',
              '[class*="item-label"]', '[class*="question-title"]', '[class*="questionTitle"]',
              '[class*="label"]', '[class*="title"]'
            ].join(', ');
            const container = semanticContainer(el);
            if (container) {
              for (const node of [...container.querySelectorAll(labelLike)].slice(0, 24)) {
                if (node === el || node.contains(el)) continue;
                push(labelText(node));
              }
              for (const child of [...container.children].slice(0, 24)) {
                if (child === el || child.contains(el) || child.querySelector(controlSelector)) continue;
                push(labelText(child));
              }
            }
            let node = el;
            for (let depth = 0; node && depth < 6 && node !== document.body; depth += 1, node = node.parentElement) {
              let sibling = node.previousElementSibling;
              for (let count = 0; sibling && count < 3; count += 1, sibling = sibling.previousElementSibling) {
                if (!sibling.querySelector(controlSelector)) push(labelText(sibling));
              }
            }
            return values.slice(0, 8);
          };
          const knownSectionTitles = new Set(['个人信息','个人基本信息','基本信息','教育经历','教育背景',
            '实习经历','实习经验','学生实践经验','项目经历','项目经验','语言能力','IT技能','技能',
            '工作经历','正式工作经历','个人荣誉','获奖情况','证书','简历附件','附件',
            '家庭成员及社会关系','其他','其他信息','诚信声明']);
          const sectionPathFor = el => {
            const values = [];
            const push = value => {
              const text = normalizeFieldText(value);
              if (meaningfulFieldText(text) && text.length <= 160 && !values.includes(text)) values.push(text);
            };
            const sectionSelector = [
              ...(profile?.section_selectors || []),
              'legend', 'h1', 'h2', 'h3', 'h4', 'h5', '[role="heading"]',
              '[class*="section-title"]', '[class*="sectionTitle"]'
            ].join(', ');
            // An Ant resume section has its own direct header before its
            // content. This is section evidence only, not a record identity.
            const resumeSection = el.closest('.resume-tpl-wrap');
            if (resumeSection && rendered(resumeSection)) {
              const sectionCaption = child => normalizeFieldText(labelText(child))
                .replace(/\s*[+＋]?\s*添加\s*$/, '').trim();
              const titles = [...resumeSection.children].filter(child =>
                rendered(child) && !child.contains(el) && !child.matches('nav,[role="navigation"],'+helperSelector) &&
                !child.querySelector(controlSelector+', '+customSelector) &&
                (child.matches('.resume-tpl-title,.resume-tpl-header,.resume-tpl-head') ||
                 knownSectionTitles.has(sectionCaption(child))));
              const captions = titles.map(sectionCaption)
                .filter(text=>meaningfulFieldText(text) && text.length<=160);
              if (new Set(captions).size===1 && titles.every(title=>
                  Boolean(title.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING))) push(captions[0]);
            }
            let node = semanticContainer(el) || el.parentElement;
            for (let depth = 0; node && depth < 9 && node !== document.body; depth += 1, node = node.parentElement) {
              const directHeading = [...node.children].find(child => child.matches?.(sectionSelector));
              if (directHeading && !directHeading.contains(el)) push(labelText(directHeading));
              let sibling = node.previousElementSibling;
              for (let count = 0; sibling && count < 3; count += 1, sibling = sibling.previousElementSibling) {
                if (sibling.matches?.(sectionSelector)) {
                  push(labelText(sibling)); break;
                }
              }
            }
            return values.reverse().slice(-8);
          };
          const helpTextFor = el => {
            const ids = clean((inputFor(el)?.getAttribute('aria-describedby') || el.getAttribute('aria-describedby')))
              .split(/\s+/).filter(Boolean);
            const described = ids.map(id => labelText(document.getElementById(id))).filter(Boolean);
            const container = semanticContainer(el);
            const helperSelector = [
              '.help', '.hint', '.tip', '.description', '.ant-form-item-extra', '.ant-form-item-explain',
              '.arco-form-item-extra', '.el-form-item__error', '[class*="help-text"]',
              '[class*="field-help"]', '[class*="form-tip"]', 'small'
            ].join(', ');
            const nearby = [...(container?.querySelectorAll(helperSelector) || [])]
              .filter(node => node !== el && !node.contains(el)).map(labelText).filter(Boolean);
            return [...new Set([...described, ...nearby])].join(' / ').slice(0, 500);
          };
          const choiceSelectorFor = fieldType => fieldType === 'radio'
            ? radioSelector
            : 'input[type="checkbox"], [role="checkbox"]';
          const visibleChoices = (root, fieldType) => [...(root?.querySelectorAll(choiceSelectorFor(fieldType)) || [])]
            .filter(item => !item.disabled && !item.closest('[hidden],[aria-hidden="true"]') &&
              (rendered(item) || rendered(item.closest('label')) || rendered(item.parentElement)));
          const commonAncestor = nodes => {
            let candidate = nodes[0] || null;
            while (candidate && !nodes.every(node => candidate.contains(node))) candidate = candidate.parentElement;
            return candidate;
          };
          const checkboxGroupSelector = '[role="group"],.ant-checkbox-group,.arco-checkbox-group,.el-checkbox-group,.ivu-checkbox-group,[class*="checkbox-group"]';
          const radioGroupSelector = '.phoenix-radio-group,.ant-radio-group,.arco-radio-group,.el-radio-group,.ivu-radio-group,[role="radiogroup"],[role="group"]';
          const declaredChoiceGroup = (target, fieldType) => {
            const selector=fieldType==='radio' ? radioGroupSelector : checkboxGroupSelector;
            for (let node=target.parentElement; node && node!==document.body; node=node.parentElement) {
              if (!choiceOptionWrapper(node) && node.matches(selector)) return node;
              if (node.matches('form,header,nav,[role="navigation"]')) break;
            }
            return null;
          };
          const onlyChoiceMembers = (root, members) => {
            const expected = new Set(members.map(canonical));
            const logical = [...new Set([...root.querySelectorAll(controlSelector+', '+customSelector)]
              .filter(item=>item.type!=='hidden' && !item.disabled && renderedControl(item)).map(canonical))];
            return logical.length===expected.size && logical.every(item=>expected.has(item));
          };
          const declarationText = /承诺|声明|真实可信|真实性|我保证|法律责任|协议|条款|隐私|consent|privacy|terms|legal|declaration|attest|certify|acknowledge|undertaking/i;
          const singleChoiceStatement = el => {
            const target = inputFor(el) || el;
            let node = target.closest('label') || target.parentElement;
            for (let depth=0; node && depth<5 && node!==document.body; depth++,node=node.parentElement) {
              if (node.matches('header,nav,[role="navigation"]')) return null;
              const logical = [...new Set([...node.querySelectorAll(controlSelector+', '+customSelector)]
                .filter(item=>item.type!=='hidden' && !item.disabled && renderedControl(item)).map(canonical))];
              if (logical.length!==1 || logical[0]!==canonical(el)) return null;
              const parts = node.matches('label') ? [node] : [...node.children].filter(child=>
                !child.contains(target) && !child.matches(helperSelector) &&
                !child.querySelector(controlSelector+', '+customSelector) && rendered(child));
              const captions = parts.map(part=>({node:part,text:normalizeFieldText(labelText(part))}))
                .filter(part=>meaningfulFieldText(part.text) && part.text.length<=300 && declarationText.test(part.text));
              const distinct = [...new Map(captions.map(part=>[normalizedToken(part.text),part])).values()];
              if (distinct.length===1) return {node,caption:distinct[0].text,captionNode:distinct[0].node};
            }
            return null;
          };
          const boundedCheckboxMembers = (members, target) => {
            const choices=members.filter(member=>!singleChoiceStatement(member));
            if (!choices.includes(target)) return [];
            const root=commonAncestor(choices);
            if (!root || root===document.body || root===document.documentElement) return [];
            const independentGroup=[...root.querySelectorAll(checkboxGroupSelector)]
              .some(group=>!choiceOptionWrapper(group) && !group.contains(target) && visibleChoices(group,'checkbox').length);
            const independentTitle=[...root.querySelectorAll(questionTitleSelector)]
              .some(caption=>{
                let owner=caption.parentElement;
                while(owner && owner!==root && !visibleChoices(owner,'checkbox').length) owner=owner.parentElement;
                return owner && owner!==root && !owner.contains(target) && visibleChoices(owner,'checkbox').length;
              });
            const owners=new Set(choices.map(member=>fieldContainer(member))
              .filter(owner=>owner && owner.querySelector(questionTitleSelector)));
            return independentGroup || independentTitle || owners.size>1 ? [] : choices;
          };
          const choiceMembersFor = (el, fieldType) => {
            const input = inputFor(el);
            const target = input && (input.type || '').toLowerCase() === fieldType ? input : el;
            if (fieldType==='checkbox' && singleChoiceStatement(el)) return [el];
            const declared = declaredChoiceGroup(target,fieldType);
            const fieldOwner=fieldContainer(target);
            const local = declared || (fieldType!=='checkbox' || fieldOwner?.querySelector(questionTitleSelector) ? fieldOwner : null) || ownedQuestion(target)?.node ||
              (fieldType==='radio' ? entityContainerFor(target) : null);
            // A declared owned group outranks native name: components may
            // omit names or reuse them across records, and an option's name
            // must not truncate the observed group to a singleton.
            const observedMembers=visibleChoices(declared,fieldType);
            if (declared && observedMembers.length && !onlyChoiceMembers(declared,observedMembers)) return [el];
            if (declared && fieldType==='radio') {
              const names=observedMembers.map(member=>member.matches('input[type="radio"]') ? clean(member.name) : '');
              if (names.some(Boolean) && new Set(names).size!==1) return [el];
            }
            const declaredMembers = fieldType==='checkbox' ? boundedCheckboxMembers(observedMembers,target) : observedMembers;
            if (declaredMembers.length >= 2) return declaredMembers;
            if (target.name && target.matches(`input[type="${fieldType}"]`)) {
              // Repeated education rows may reuse a name. Prefer the owned
              // question/record, not every similarly named input in the form.
              // Equal checkbox names do not establish a shared question across
              // the whole form. Radios retain native name grouping as fallback.
              const scope = local || (fieldType==='radio' ? target.form || document : null);
              let named = [...(scope?.querySelectorAll(`input[type="${fieldType}"][name="${CSS.escape(target.name)}"]`) || [])]
                .filter(item => !item.disabled && (rendered(item) || rendered(item.closest('label')) || rendered(item.parentElement)));
              if (fieldType==='checkbox') named=boundedCheckboxMembers(named,target);
              if (named.length>=2 && scope && onlyChoiceMembers(commonAncestor(named)||scope,named)) return named;
            }
            let node = el.parentElement;
            for (let depth = 0; node && depth < 9 && node !== document.body; depth += 1, node = node.parentElement) {
              const members = visibleChoices(node, fieldType);
              if (members.length >= 2 && members.length <= 20) {
                const expected = new Set(members.map(canonical));
                const logical = [...new Set([...node.querySelectorAll(controlSelector+', '+customSelector)]
                  .filter(item=>item.type!=='hidden' && !item.disabled && renderedControl(item)).map(canonical))];
                const independentGroup = [...node.querySelectorAll(fieldType==='radio' ? radioGroupSelector : checkboxGroupSelector)]
                  .some(group=>!choiceOptionWrapper(group) && !group.contains(el) && visibleChoices(group,fieldType).length);
                const independentTitle = [...node.querySelectorAll(questionTitleSelector)]
                  .some(caption=>{
                    let owner=caption.parentElement;
                    while(owner && owner!==node && !visibleChoices(owner,fieldType).length) owner=owner.parentElement;
                    return owner && owner!==node && !owner.contains(el) && visibleChoices(owner,fieldType).length;
                  });
                const owners = new Set(members.map(member=>fieldContainer(member))
                  .filter(owner=>owner && owner.querySelector(questionTitleSelector)));
                if (logical.length!==expected.size || !logical.every(item=>expected.has(item)) ||
                    independentGroup || independentTitle || owners.size>1 || node.querySelector('header,nav,[role="navigation"]')) return [el];
                return members;
              }
              if (node===local || node.matches('form,header,nav,[role="navigation"]')) break;
            }
            return [el];
          };
          const stripChoiceSuffix = (value, options) => {
            let result = clean(value);
            const tokens = [...new Set(options.map(clean).filter(Boolean))].sort((a, b) => b.length - a.length);
            let changed = true;
            while (result && changed) {
              changed = false;
              for (const token of tokens) {
                if (result === token) {
                  result = ''; changed = true; break;
                }
                if (result.endsWith(token)) {
                  const prefix = result.slice(0, -token.length);
                  if (/[\s/、，,;；|·—-]$/.test(prefix)) {
                    result = prefix.replace(/[\s/、，,;；|·—-]+$/, '').trim();
                    changed = true; break;
                  }
                }
              }
            }
            return result;
          };
          const choiceOnlyText = (value, options) => {
            let remaining = clean(value).toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]/g, '');
            const tokens = [...new Set(options.map(value => clean(value).toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]/g, '')).filter(Boolean))]
              .sort((a, b) => b.length - a.length);
            if (!remaining || !tokens.length) return false;
            let previous = null;
            while (previous !== remaining) {
              previous = remaining;
              for (const token of tokens) remaining = remaining.split(token).join('');
            }
            return !remaining;
          };
          let choiceGroupSequence = 0;
          const ownedChoiceQuestion = (group, members) => {
            const expected = new Set(members.map(canonical));
            let node = group;
            for (let depth=0; node && depth<8 && node!==document.body; depth++,node=node.parentElement) {
              const logical = [...new Set([...node.querySelectorAll(controlSelector+', '+customSelector)]
                .filter(item => item.type!=='hidden' && !item.disabled &&
                  (renderedControl(item)||rendered(item.closest('label'))||rendered(item.parentElement)))
                .map(canonical))];
              if (logical.length!==expected.size || !logical.every(item=>expected.has(item))) continue;
              const captions = [...node.children].filter(child =>
                !members.some(member=>child.contains(member)) && !child.querySelector(controlSelector+', '+customSelector) &&
                !child.matches('header,nav,[role="navigation"],.resume-tpl-title,.resume-tpl-header,.resume-tpl-head,h1,h2,h3,h4,h5,[role="heading"],'+helperSelector) && rendered(child))
                .map(child=>({node:child,text:normalizeFieldText(ownedCaptionText(child))}))
                .filter(item=>meaningfulFieldText(item.text) && item.text.length<=220 &&
                  !choiceOnlyText(item.text,members.map(member=>member.value||member.getAttribute('aria-label')||'')));
              const distinct=[...new Map(captions.map(item=>[item.text,item])).values()];
              if (distinct.length===1) return {node,caption:distinct[0].text,captionNode:distinct[0].node};
            }
            return null;
          };
          const choiceGroupCache = new WeakMap();
          const choiceGroupInfoFor = (el, fieldType) => {
            if (!['radio', 'checkbox'].includes(fieldType)) return null;
            if (choiceGroupCache.has(el)) return choiceGroupCache.get(el);
            const statement = fieldType==='checkbox' ? singleChoiceStatement(el) : null;
            if (statement) {
              const group=statement.node;
              const marker=group.getAttribute('data-zhida-choice-group') || `zhida-choice-${Date.now()}-${choiceGroupSequence++}`;
              group.setAttribute('data-zhida-choice-group',marker);
              return {group,members:[el],options:[statement.caption],question:statement.caption,
                source:'container-owned',key:marker,captionNode:statement.captionNode,
                evidence:questionEvidence([{text:statement.caption,source:'container-owned'}]),statement:statement.caption};
            }
            const members = choiceMembersFor(el, fieldType);
            let group = commonAncestor(members);
            if (!group || group === document || group === document.documentElement || group === document.body) {
              group = el.closest('[role="radiogroup"], fieldset, [role="group"]') || el.parentElement;
            }
            const options = members.map(item => clean(
              labelText(item.id ? document.querySelector(`label[for="${CSS.escape(item.id)}"]`) : null) ||
              labelText(item.closest('label')) || item.getAttribute('aria-label') || item.innerText || item.value
            )).filter(Boolean);
            const ownedChoice = ownedChoiceQuestion(group, members);
            const groupOwner = ownedChoice?.node || fieldContainer(group) || group;
            const phoenixOwner = el.closest('.form-item.form-item--phoenix');
            const phoenixOwnsGroup = phoenixOwner && [...phoenixOwner.querySelectorAll(controlSelector)]
              .every(control=>members.some(member=>canonical(member)===canonical(control)));
            const questionSelector = [
              ...(profile?.label_selectors || []),
              'legend', '.application-label', '.question-label', '[data-qa="question-label"]',
              '.ant-form-item-label', '.arco-form-label-item', '.el-form-item__label',
              '[class*="form-label"]', '[class*="field-label"]', '[class*="item-label"]',
              '[class*="formLabel"]', '[class*="fieldLabel"]', '[class*="questionTitle"]',
              '[class*="question-title"]', '[data-testid*="question"]'
            ].join(', ');
            const candidates = [
              {text:ownedChoice?.caption,source:'container-owned'},
              {text: phoenixOwner?.querySelector(':scope > .form-item__title')?.innerText,
               source:phoenixOwnsGroup ? 'container-owned' : 'nearby'},
              {text: labelledBy(group), source: 'aria-labelledby'},
              {text: group?.getAttribute('aria-label'), source: 'aria'},
              ...[...(groupOwner?.querySelectorAll(questionSelector) || [])]
                .map(node => ({text: labelText(node), source:
                  node.tagName === 'LEGEND' && node.parentElement === group ? 'explicit' :
                  (fieldContainer(node.parentElement) === groupOwner &&
                   [...groupOwner.querySelectorAll(controlSelector)].every(control=>members.some(member=>canonical(member)===canonical(control))) &&
                   !node.querySelector(controlSelector) &&
                   !members.some(member => node.contains(member)) ? 'container-owned' : 'nearby')})),
              ...nearbyLabelCandidates(group || el).map(text => ({text, source: 'nearby'})),
              {text: precedingLabel(group || el), source: 'preceding'}
            ];
            let node = group;
            for (let depth = 0; node && depth < 5 && node !== document.body; depth += 1, node = node.parentElement) {
              const choiceCount = visibleChoices(node, fieldType).length;
              const allControls = node.querySelectorAll(controlSelector).length;
              if (choiceCount === members.length && allControls <= members.length + 1) {
                candidates.push({text: labelText(node), source: 'nearby'});
              }
            }
            const cleanedCandidates = candidates.map(item => ({
              ...item, text: stripChoiceSuffix(item.text, options)
            })).filter(item => item.text && item.text.length <= 300 && !choiceOnlyText(item.text, options));
            // Unowned nearby text cannot become a checkbox question. A local
            // group caption or explicit accessible title is required; a missing
            // title is safer than borrowing a header or another question.
            const bestQuestion = bestFieldText(cleanedCandidates.filter(item=>
              ['container-owned','explicit','aria-labelledby','aria'].includes(item.source)), options);
            const question = bestQuestion.text;
            const marker = group ? (group.getAttribute('data-zhida-choice-group') ||
              `zhida-choice-${Date.now()}-${choiceGroupSequence++}`) : '';
            if (group && marker) group.setAttribute('data-zhida-choice-group', marker);
            if (group && question) group.setAttribute('data-zhida-choice-label', question);
            const result = {group, members, options, question, source: bestQuestion.source, key: marker,
              captionNode:ownedChoice?.captionNode,
              evidence:questionEvidence(cleanedCandidates)};
            members.forEach(member=>choiceGroupCache.set(member,result));
            return result;
          };
          const contextFor = el => {
            const preferred = semanticContainer(el);
            const text = labelText(preferred);
            if (text && text.length <= 600) return text;
            let node = el.parentElement;
            for (let depth = 0; node && depth < 7 && node !== document.body; depth += 1, node = node.parentElement) {
              const candidate = labelText(node);
              const controls = node.querySelectorAll(controlSelector).length;
              if (candidate && candidate.length <= 600 && controls <= 6) return candidate;
            }
            return '';
          };
          const labelInfoFor = (el, fieldType, choiceInfo) => {
            const input = inputFor(el);
            const target = input || el;
            const explicit = target.id ? document.querySelector(`label[for="${CSS.escape(target.id)}"]`) : null;
            const wrapping = target.closest('label') || el.closest('label');
            const wrappingOwnsTarget = wrapping &&
              [...new Set([...wrapping.querySelectorAll(controlSelector+', '+customSelector)].map(canonical))]
                .every(control=>control===canonical(el));
            const question = semanticContainer(el);
            const questionLabel = question?.querySelector('.application-label, legend, .question-label, [data-qa="question-label"]');
            const optionLabel = labelText(explicit) || labelText(wrapping);
            const groupLabel = clean(choiceInfo?.question || questionLabel?.innerText || nearbyLabel(el));
            const internalName = (target.getAttribute('name') || '').includes('[');
            if (choiceInfo?.question) {
              const ownedSource=['explicit','aria-labelledby','container-owned','aria'].includes(choiceInfo.source);
              return {text: clean(choiceInfo.question), source: choiceInfo.source || 'nearby',
                confidence:ownedSource ? .96 : .6, evidence:choiceInfo.evidence};
            }
            if (['radio','checkbox'].includes(fieldType)) {
              return {text:fieldType==='radio' ? '未识别的单选题' : '未识别的复选题',
                source:'generated',confidence:.1,evidence:choiceInfo?.evidence || []};
            }
            const owned = ownedQuestion(el);
            // ATS components often use a div instead of label[for]. Treat it
            // as owned only when this question container has exactly one
            // logical control and one distinct label belonging to that same
            // container. Canonicalisation collapses a combobox wrapper and its
            // nested input; adjacent fields and nested question labels cannot
            // acquire this provenance merely because they are nearby.
            const ownedContainerLabel = () => {
              if (!question) return '';
              const controls = [...question.querySelectorAll(controlSelector + ', ' + customSelector)]
                .filter(item => item.type !== 'hidden' && !item.disabled);
              const logical = [...new Set(controls.map(canonical))];
              if (logical.length !== 1 || logical[0] !== canonical(el)) return '';
              const selectors = [
                ...(profile?.label_selectors || []), 'label', 'legend', '.application-label',
                '.question-label', '[data-qa="question-label"]', '.ant-form-item-label',
                '.arco-form-label-item', '.el-form-item__label', '[class*="form-label"]',
                '[class*="field-label"]', '[class*="item-label"]', '[class*="formLabel"]',
                '[class*="fieldLabel"]', '[class*="questionTitle"]'
              ].join(', ');
              const labels = [...question.querySelectorAll(selectors)].filter(node => {
                if (node === el || node.contains(el) || node.querySelector(controlSelector + ', ' + customSelector)) return false;
                const owner = (node.parentElement && fieldContainer(node.parentElement)) || semanticContainer(node);
                return owner === question;
              }).map(node => normalizeFieldText(ownedCaptionText(node)))
                .filter(text => meaningfulFieldText(text) && text.length <= 300);
              const distinct = [...new Map(labels.map(text => [normalizedToken(text), text])).values()];
              return distinct.length === 1 ? distinct[0] : '';
            };
            const candidates = [
              {text: ownedCaptionText(explicit), source: 'explicit'},
              {text: ownedCaptionText(wrapping), source: wrappingOwnsTarget ? 'explicit' : 'nearby'},
              {text: labelledBy(target) || labelledBy(el), source: 'aria-labelledby'},
              {text: owned?.caption || ownedContainerLabel(), source: 'container-owned'},
              {text: groupLabel || nearbyLabel(el), source: 'nearby'},
              {text: precedingLabel(el), source: 'preceding'},
              ...nearbyLabelCandidates(el).map(text => ({text, source: 'nearby'})),
              {text: target.getAttribute('aria-label') || el.getAttribute('aria-label'), source: 'aria'},
              {text: target.getAttribute('data-label') || el.getAttribute('data-label'), source: 'attribute'},
              {text: target.getAttribute('title') || el.getAttribute('title'), source: 'attribute'},
              {text: target.getAttribute('placeholder') || el.getAttribute('placeholder'), source: 'placeholder'},
              {text: target.getAttribute('name') || el.getAttribute('name'), source: 'name'},
            ];
            if ((internalName || ['file'].includes(target.type)) && groupLabel) {
              candidates.unshift({text: groupLabel, source: 'nearby'});
            }
            const titleCandidates = ['file','checkbox'].includes(fieldType) ? candidates.filter(item=>
              ['explicit','aria-labelledby','container-owned','aria'].includes(item.source) &&
              !/^(?:上传文件|选择文件|文件大小|upload file|choose file|file size)(?:$|[\s:：]|大小|不能|不应|不得)/i.test(normalizeFieldText(item.text))) : candidates;
            const candidate = bestFieldText(titleCandidates, choiceInfo?.options || []);
            if (!candidate.text && ['file','checkbox'].includes(fieldType)) {
              // Keep this explicit gap through the legacy metadata finalizer;
              // it must not replace a missing local caption with a help tip or
              // a distant header from nearby_labels/context.
              return {text:fieldType==='file' ? '未识别的附件题目' : '未识别的复选题',
                source:'generated',confidence:.1,evidence:questionEvidence(candidates)};
            }
            const confidence = candidate.source === 'container-owned' ? .96 :
              Math.max(.1, Math.min(.99, (candidate.score + 20) / 85));
            return {text: candidate.text, source: candidate.source, confidence,
              evidence:questionEvidence(candidates)};
          };
          const groupLabelFor = (el, choiceInfo) => {
            if (choiceInfo?.question) return clean(choiceInfo.question);
            const group = semanticContainer(el);
            return clean(group?.querySelector('.application-label, legend, .question-label, [data-qa="question-label"], .ant-form-item-label, .arco-form-label-item, .el-form-item__label, [class*="form-label"], [class*="field-label"], [class*="formLabel"], [class*="fieldLabel"], [class*="questionTitle"]')?.innerText || nearbyLabel(el));
          };
          const optionLabelFor = (el, choiceInfo) => {
            const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
            return clean(labelText(explicit) || labelText(el.closest('label')) || el.getAttribute('aria-label') || el.innerText || choiceInfo?.statement || el.value);
          };
          const sectionFor = el => {
            const entity = entityContainerFor(el);
            const entityHeading = entity ? [...entity.children]
              .find(child => child.matches?.('legend, h1, h2, h3, h4, [role="heading"], [class*="title"]')) : null;
            if (entityHeading) return clean(entityHeading.innerText);
            const section = el.closest('fieldset, section, [role="group"], .application-section, .form-section');
            const heading = section?.querySelector('legend, h1, h2, h3, h4, [role="heading"]');
            return clean(heading?.innerText);
          };
          const candidates = [...document.querySelectorAll(
            controlSelector + ', ' + customSelector
          )];
          const canonical = el => {
            // A custom radio wrapper and its native input are one target, not
            // two answers. Role-only widgets without native inputs stay intact.
            if (el.matches('[role="radio"],[role="checkbox"]')) {
              const type=el.getAttribute('role');
              const native=el.querySelector(`input[type="${type}"]`);
              if (native) return native;
            }
            // Select2's hidden native select is 1px, not display:none. It and
            // the adjacent visible combobox are one logical control.
            if (el.matches('select.select2-hidden-accessible') && el.nextElementSibling?.matches('.select2-container')) {
              const widget = el.nextElementSibling.querySelector('[role="combobox"]');
              if (widget) return widget;
            }
            // Generic '*picker-input*' also matches the readonly child of a
            // calendar. Prefer its proven outer widget, deduplicate once, and
            // retain the outer control as the commit/readback target.
            const structured = el.closest('.ant-calendar-picker, .ant-picker, .ant-cascader-picker, .ant-cascader');
            if (structured) return structured;
            const wrapper = el.closest(customWrapperSelector);
            if (wrapper) return wrapper;
            if (el.matches(customSelector)) return el;
            const root = el.closest(customSelector);
            if (root) return root;
            if (el.tagName === 'INPUT' && (el.readOnly || el.getAttribute('aria-autocomplete'))) {
              const widget = el.closest('.ant-select, .arco-select, .el-select, [class*="select"], [class*="cascader"], [class*="picker"]');
              if (widget) return widget.querySelector(customSelector) || widget;
            }
            return el;
          };
          const allLogical = [...new Set(candidates.map(canonical))];
          const inventoryLogical = [...new Set([...allLogical, ...document.querySelectorAll(
            '[role="textbox"],[contenteditable="true"],[aria-haspopup="tree"],[aria-haspopup="dialog"]')].map(canonical))];
          const visibleLogical = inventoryLogical.filter(el => {
            const target=inputFor(el)||el;
            return !el.closest('[hidden],[aria-hidden="true"]') &&
              (target.type==='file' || rendered(el) || rendered(el.closest('label')) || rendered(el.parentElement));
          });
          const elements = allLogical
            .filter(el => {
              const role = (el.getAttribute('role') || '').toLowerCase();
              const type = (el.type || '').toLowerCase();
              const customSelect = role === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox' || el.matches(customSelector);
              const optionInput = ['radio', 'checkbox'].includes(type) || ['radio', 'checkbox'].includes(role);
              if (el.closest('header,nav,[role="navigation"],[role="search"]')) return false;
              if (!customSelect && !optionInput && ['hidden','submit','button','image','reset','password'].includes(type)) return false;
              const optionVisible = optionInput && (el.closest('label')?.getClientRects().length || el.parentElement?.getClientRects().length);
              const target = inputFor(el) || el;
              if (el.closest('.phoenix-date-picker, .phoenix-selectList__list, .ant-calendar, .ant-picker-dropdown, .ant-select-dropdown, .ant-cascader-menus')) return false;
              const search = /^(?:搜索职位关键词|搜索職位關鍵詞|搜索岗位|search for job keywords)$/i.test(clean(target.getAttribute('placeholder'))) || type === 'search';
              if (search && (el.closest('header, nav, [role="search"], [role="navigation"]') ||
                  (!fieldContainer(el) && !ownedQuestion(el)))) return false;
              if (target.tagName === 'TEXTAREA' && target.readOnly && !target.labels?.length &&
                  !fieldContainer(el) && !ownedQuestion(el)) return false;
              return !el.disabled && !target.disabled && !el.closest('[hidden],[aria-hidden="true"]') &&
                (type === 'file' || rendered(el) || (optionVisible && rendered(el.parentElement)));
            });
          let containerSequence = 0;
          const fields = elements.map((el, index) => {
            const marker = el.getAttribute('data-zhida-field') ||
              `zhida-${Date.now()}-${index}-${Math.random().toString(36).slice(2)}`;
            el.setAttribute('data-zhida-field', marker);
            const input = inputFor(el);
            const role = (el.getAttribute('role') || '').toLowerCase();
            const customSelect = role === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox' || el.matches(customSelector);
            const fieldType = customSelect ? 'combobox' : (el.matches(radioSelector) ? 'radio' : (role === 'checkbox' ? role : (el.type || el.tagName).toLowerCase()));
            const choiceInfo = choiceGroupInfoFor(el, fieldType);
            const labelInfo = labelInfoFor(el, fieldType, choiceInfo);
            const label = labelInfo.text.slice(0, 500);
            const nearbyLabels = nearbyLabelCandidates(el);
            const sectionPath = sectionPathFor(el);
            const entityContainer = entityContainerFor(choiceInfo?.group || el);
            const containerMarker = entityContainer ? (entityContainer.getAttribute('data-zhida-container') ||
              `zhida-container-${Date.now()}-${containerSequence++}`) : '';
            if (entityContainer && containerMarker) entityContainer.setAttribute('data-zhida-container', containerMarker);
            const context = clean(choiceInfo?.question || contextFor(el)).slice(0, 600);
            const placeholderText = clean(input?.getAttribute('placeholder') || el.getAttribute('placeholder')).slice(0, 300);
            let options = input?.tagName === 'SELECT' ? [...input.options].filter(o => !o.disabled && !o.parentElement?.disabled).map(o => clean(o.text)).filter(value => value && !placeholder(value)) : [];
            if (['radio', 'checkbox'].includes(fieldType)) {
              options = choiceInfo?.options || [];
            }
            const owner = fieldContainer(el) || semanticContainer(el);
            const requiredEvidence = [];
            if (el.required || input?.required) requiredEvidence.push('HTML required');
            const ownerControls = owner ? [...new Set([...owner.querySelectorAll(controlSelector+', '+customSelector)].map(canonical))] : [];
            const ownerBelongs = ownerControls.length>0 && ownerControls.every(control=>
              choiceInfo ? choiceInfo.members.some(member=>canonical(member)===control) : canonical(el)===control);
            if ([el,input,choiceInfo?.group,ownerBelongs?owner:null].some(node => node?.getAttribute('aria-required')==='true'))
              requiredEvidence.push('ARIA required');
            // Asterisks are checked only on this question's owned caption, not
            // the text of a whole section or another field's validation error.
            // Deep Ant templates place their title beside the inner form row,
            // outside .ant-form-item. Reuse the exact caption node already
            // proven to own this control/choice group; a nearby star is not
            // permission to mark another question required.
            let ownedCaption = choiceInfo?.captionNode || ownedQuestion(el)?.captionNode;
            if (choiceInfo?.statement) {
              // A statement's option label can be inside the input wrapper;
              // its required star may live on the matching question caption.
              // Look only within a proven singleton owner, never the section.
              for (let node=el.parentElement,depth=0; node && node!==document.body && depth<12; node=node.parentElement,depth++) {
                if (!onlyChoiceMembers(node,choiceInfo.members)) break;
                const captions=[...node.children].filter(child=>child.matches(questionTitleSelector) &&
                  !child.contains(el) && !child.querySelector(controlSelector+', '+customSelector) && rendered(child) &&
                  normalizedToken(ownedCaptionText(child))===normalizedToken(labelInfo.text));
                if (captions.length===1) {ownedCaption=captions[0];break;}
              }
            }
            const exactOwnedCaption = ownedCaption &&
              normalizedToken(ownedCaptionText(ownedCaption))===normalizedToken(labelInfo.text) ? ownedCaption : null;
            const ownedLabel = (input?.id ? document.querySelector(`label[for="${CSS.escape(input.id)}"]`) : null) ||
              exactOwnedCaption ||
              (ownerBelongs ? [...(owner?.querySelectorAll('.ant-form-item-label, .el-form-item__label, .arco-form-label-item, :scope > label, :scope > legend')||[])]
                .find(node=>normalizedToken(ownedCaptionText(node))===normalizedToken(labelInfo.text)) : null);
            if (ownedLabel && (/[＊*✱]/.test(ownedCaptionText(ownedLabel)) ||
                /[＊*✱]/.test(getComputedStyle(ownedLabel,'::before').content) ||
                /[＊*✱]/.test(getComputedStyle(ownedLabel,'::after').content) ||
                ownedLabel.matches('.ant-form-item-required,[aria-required="true"]') ||
                [...ownedLabel.querySelectorAll('.ant-form-item-required,.required,[class*="required-mark"]')]
                  .some(mark=>rendered(mark) && !mark.closest(helperSelector))))
              requiredEvidence.push('当前题目标签的必填标记');
            const lengthAttribute = key => {
              const raw=(input||el).getAttribute(key);
              return raw!==null && /^\\d+$/.test(raw) ? Number(raw) : null;
            };
            // Custom options are read through the same scoped tool used by execution.
            const selectedItems = customSelect ? [...el.querySelectorAll('[class*="selection-item"], [class*="selected-value"], [class*="selected-item"], .ant-select-selection-selected-value')]
              .map(item => clean(item.innerText || item.textContent)).filter(value => value && !placeholder(value)) : [];
            const phoenixContent = el.matches('.phoenix-select') ? el.querySelector('.phoenix-select__content') : null;
            const customValue = phoenixContent ? clean(phoenixContent.innerText) :
              selectedItems.join(', ') || clean(el.getAttribute('aria-valuetext') || el.querySelector('input')?.value || el.innerText);
            return {
              selector: `[data-zhida-field="${marker}"]`, label, question_text: label,
              label_source: labelInfo.source, recognition_confidence: labelInfo.confidence || 0,
              question_candidates:labelInfo.evidence || [],
              context, help_text: helpTextFor(el), nearby_labels: nearbyLabels,
              section_path: sectionPath, placeholder: placeholderText, ordinal: index + 1,
              name: el.getAttribute('name') || el.querySelector('input')?.getAttribute('name') || el.getAttribute('id') || '', field_type: fieldType,
              required: Boolean(requiredEvidence.length>0 || choiceInfo?.members.some(item => item.required)),
              required_evidence:requiredEvidence, options,
              options_capture:input?.tagName==='SELECT' ? 'native_complete' :
                ['radio','checkbox'].includes(fieldType) ? 'group_complete' : '',
              constraints:{input_type:(input?.type||'').toLowerCase(), input_mode:input?.getAttribute('inputmode')||'',
                pattern:input?.getAttribute('pattern')||'', min_length:lengthAttribute('minlength'),
                max_length:lengthAttribute('maxlength'), minimum:input?.getAttribute('min')||'',
                maximum:input?.getAttribute('max')||'', step:input?.getAttribute('step')||''},
              current_value: el.type === 'file' ? [...(el.files || [])].map(file => file.name).join(', ') :
                (input?.tagName === 'SELECT' ? [...input.selectedOptions]
                  .map(option => clean(option.textContent || option.value)).filter(value => value && !placeholder(value)).join(', ') :
                (['checkbox','radio'].includes(fieldType) ? String(el.checked ?? el.getAttribute('aria-checked') === 'true') :
                  clean(el.value || (customSelect ? customValue : '')))),
              accept: el.getAttribute('accept') || '', role,
              group_label: (fieldType==='checkbox' ? choiceInfo?.question || '' : groupLabelFor(el, choiceInfo)).slice(0, 500),
              option_label: ['radio','checkbox'].includes(fieldType) ? optionLabelFor(el,choiceInfo).slice(0, 500) : '',
              option_value: ['radio','checkbox'].includes(fieldType) ? clean(el.value || el.getAttribute('data-value') || el.innerText) : '',
              multiple: Boolean(el.multiple || el.getAttribute('aria-multiselectable') === 'true' ||
                /multiple|multi|tags/.test(String(el.className || '').toLowerCase()) ||
                /multiple|multi|tags/.test(String(el.closest('[class]')?.className || '').toLowerCase())),
              readonly: Boolean(el.readOnly || el.querySelector('input')?.readOnly || el.getAttribute('aria-readonly') === 'true'),
              autocomplete: input?.getAttribute('autocomplete') || el.getAttribute('autocomplete') || '',
              section: (sectionPath[sectionPath.length - 1] || sectionFor(el) || '').slice(0, 500), container_key: containerMarker,
              control_group_key: choiceInfo?.key || ''
            };
          });
          const addPattern = /^(添加|新增|补充|add|new)(\\s|$|一条|经历|项目|技能|语言|证书|奖项)/i;
          const semanticPattern = /(技能|语言|教育|学历|经历|项目|证书|奖项|skill|language|education|experience|project|certificate|award)/i;
          const expanders = [];
          const seenExpanders = new Set();
          for (const button of [...document.querySelectorAll('button, [role="button"]')]) {
            if (!button.getClientRects().length || button.disabled) continue;
            const buttonText = clean(button.innerText || button.textContent || button.getAttribute('aria-label'));
            if (!addPattern.test(buttonText)) continue;
            let container = button.parentElement;
            let heading = '';
            for (let depth = 0; container && depth < 7; depth += 1, container = container.parentElement) {
              heading = clean(container.querySelector('h1, h2, h3, h4, legend, [role="heading"], [class*="title"]')?.innerText);
              if (semanticPattern.test(`${heading} ${buttonText}`)) break;
            }
            const semanticLabel = clean(`${heading} ${buttonText}`);
            if (!container || !semanticPattern.test(semanticLabel) || seenExpanders.has(semanticLabel)) continue;
            const existing = [...container.querySelectorAll('input, select, textarea, ' + customSelector)]
              .map(canonical).some(control => control.getClientRects().length > 0 && !control.disabled);
            if (existing) continue;
            seenExpanders.add(semanticLabel);
            const marker = button.getAttribute('data-zhida-field') ||
              `zhida-expand-${Date.now()}-${expanders.length}-${Math.random().toString(36).slice(2)}`;
            button.setAttribute('data-zhida-field', marker);
            expanders.push({
              selector: `[data-zhida-field="${marker}"]`, label: semanticLabel, question_text: semanticLabel,
              label_source: 'explicit', recognition_confidence: .98, context: semanticLabel,
              help_text: '', nearby_labels: [], section_path: heading ? [heading] : [],
              placeholder: '', ordinal: fields.length + expanders.length + 1,
              name: button.getAttribute('name') || button.getAttribute('id') || '',
              field_type: 'section-button', required: false, options: [], current_value: '',
              accept: '', role: 'button', group_label: heading, option_label: '',
              option_value: '', multiple: false, readonly: false, autocomplete: '', section: heading,
              container_key: '', control_group_key: ''
            });
          }
          const embedded = [...document.querySelectorAll('iframe,frame')].filter(rendered).length;
          // querySelectorAll does not cross a Shadow boundary. Enumerate open
          // roots recursively for COVERAGE ONLY, including nested hosts. Do
          // not read labels, values or component/private state inside them.
          const surfaceControlSelector = 'input:not([type="hidden"]),select,textarea,[role="combobox"],[role="radio"],[role="checkbox"],[role="textbox"],[contenteditable="true"]';
          const composedRendered = el => {
            if (!el || !el.getClientRects().length) return false;
            for (let node=el; node && node!==document; node=node.parentElement || node.getRootNode()?.host) {
              if (node.matches?.('[hidden],[aria-hidden="true"],[inert]')) return false;
              const style=getComputedStyle(node);
              if (style.display==='none' || ['hidden','collapse'].includes(style.visibility)) return false;
            }
            return true;
          };
          let shadow = 0;
          const inspectShadowSurfaces = root => {
            for (const host of root.querySelectorAll('*')) {
              const openRoot=host.shadowRoot;
              if (!openRoot) continue;
              if ([...openRoot.querySelectorAll(surfaceControlSelector)].some(composedRendered)) shadow += 1;
              inspectShadowSurfaces(openRoot);
            }
          };
          inspectShadowSurfaces(document);
          const pending = [];
          for (const node of document.querySelectorAll('[aria-expanded="false"],[role="tab"][aria-selected="false"]')) {
            const caption=clean(node.innerText||node.getAttribute('aria-label'));
            if (rendered(node) && caption && caption.length<100 && semanticPattern.test(caption)) pending.push(caption);
          }
          // Known native/observed section boundaries can contain a folded
          // record without ARIA expanded metadata. Its own direct heading is
          // evidence of an unread region, not permission to borrow an ancestor
          // title or click an Add/expand control. Never inspect hidden values.
          const surfaceSections = 'section,fieldset,.resume-tpl-wrap';
          const popupSurfaces = '.phoenix-date-picker,.phoenix-selectList__list,.ant-calendar,.ant-picker-dropdown,.ant-select-dropdown,.ant-cascader-menus,.ant-cascader-dropdown';
          const capturedSurfaceControls = new Set(fields.map(field=>document.querySelector(field.selector)).filter(Boolean));
          for (const section of document.querySelectorAll(surfaceSections)) {
            const headings=[...section.children].filter(node => rendered(node) &&
              (section.matches('.resume-tpl-wrap') ||
                node.matches('legend,h1,h2,h3,h4,h5,[role="heading"],.section-title,[class*="section-title"],[class*="sectionTitle"]')) &&
              !node.querySelector(controlSelector+', '+customSelector))
              .map(node=>clean(labelText(node)).replace(/\s*[+＋]?\s*添加\s*$/,'').trim());
            const titles=[...new Set(headings.filter(title=>knownSectionTitles.has(title)))];
            if (titles.length!==1) continue;
            const title=titles[0];
            const ownControls=[...new Set([...section.querySelectorAll(controlSelector+', '+customSelector)]
              .map(canonical))].filter(control=>control.closest(surfaceSections)===section &&
                !control.closest(popupSurfaces) &&
                !['hidden','password','submit','button','image','reset'].includes((inputFor(control)?.type||control.type||'').toLowerCase()));
            if (ownControls.some(control=>!rendered(control) && !capturedSurfaceControls.has(control))) {
              pending.push(title+'（已发现未呈现控件）');
              continue;
            }
            const ownAdd=[...section.querySelectorAll('button,[role="button"]')].some(button=>
              button.closest(surfaceSections)===section && rendered(button) && !button.disabled &&
              !button.matches('[aria-disabled="true"]') && !button.querySelector(controlSelector) &&
              addPattern.test(clean(button.innerText||button.textContent||button.getAttribute('aria-label'))));
            if (!ownControls.length && ownAdd) pending.push(title+'（有新增入口，记录尚未呈现）');
          }
          const excluded = visibleLogical.filter(el => !elements.includes(el) && (
            (['hidden','password','submit','button','image','reset'].includes((inputFor(el)?.type||el.type||'').toLowerCase()) &&
              !el.hasAttribute('aria-haspopup')) ||
            el.disabled || inputFor(el)?.disabled || el.closest('header,nav,[role="search"],[role="navigation"],.ant-picker-dropdown,.ant-calendar,.ant-select-dropdown,.ant-cascader-menus')
          )).length;
          return {fields:[...fields, ...expanders], inventory:{
            observed_controls:visibleLogical.length,
            captured_controls:fields.length, intentionally_excluded_controls:excluded,
            embedded_regions:embedded, unread_shadow_regions:shadow,
            pending_sections:[...new Set(pending)].slice(0,30)}};
        }
        """, recognition_profile)
        # Compatibility for existing isolated transports. Missing inventory
        # remains unknown coverage, never a fabricated completeness claim.
        data = captured.get('fields', []) if isinstance(captured, dict) else captured
        inventory = captured.get('inventory', {}) if isinstance(captured, dict) else {}
        await self._assert_snapshot_document(document_url, phase="question_capture")
        # Component libraries often render options only after the combobox opens.
        # Opening a list is read-only and lets the review UI present the real choices.
        await classify_controls(self.page, data)
        await self._assert_snapshot_document(document_url, phase="control_classification")
        self._option_probe_shapes = {}
        # Native Safari incurs an Apple Events round trip per locator operation.
        # Discovering every optional menu serially must not prevent reading the
        # already rendered form or filling independently proven text/choices.
        # No option cache: dependent menus are always re-read at execution time.
        safari_preview = isinstance(self.context, SafariContext)
        preview_deadline = time.monotonic() + 10 if safari_preview else None
        for item in data:
            locator = self.page.locator(item["selector"]).first
            if policy.name == "beisen-italent" and item.get("field_type") == "combobox":
                item["region_picker"] = await locator.get_attribute("data-zhida-region-picker") == "true"
                if item['region_picker']:
                    item['region_value_path'] = await audited_region_path(locator, item.get('current_value', ''))
                    if item['region_value_path']:
                        # Do not reopen a known reset-on-open tree and destroy
                        # its transient operation evidence. Later user input or
                        # value/question changes invalidate the audit.
                        item['options'] = []
                        continue
                item["date_precision"] = await locator.get_attribute("data-zhida-date-precision") or ""
                if item["date_precision"]:
                    item["options"] = []
                    item["help_text"] = "网页使用日历输入，填写真实日期后核验外层控件的已选值"
                    continue
            if not probe_options and item.get("field_type") == "combobox" and not item.get("options"):
                self._mark_deferred_options(item)
                continue
            if not probe_options or item.get("field_type") != "combobox" or item.get("options"):
                continue
            # Even explicit bulk discovery may not click an unowned question,
            # a declaration, or an unknown widget. Diagnosis cannot grant a
            # broader operation capability than mapping/execution.
            probe_field = PageField.model_validate(item)
            annotate_fields([probe_field])
            if (extraction_block_reason(probe_field) or is_declaration(probe_field)
                    or probe_field.control_kind == "unknown"):
                self._mark_deferred_options(item)
                continue
            if item.get('control_kind') == 'cascade':
                # A root province is not a selectable complete address. Never
                # flatten nested menus into an ordinary select option list.
                item['help_text'] = '网页是级联选择；需要完整路径，逐层读取后核验最终选中值'
                continue
            remaining = preview_deadline - time.monotonic() if safari_preview else None
            if safari_preview and remaining <= 0:
                self._mark_deferred_options(item)
                continue
            try:
                await self._assert_snapshot_document(document_url, phase="before_option_preview")
                if safari_preview:
                    # Cancel a slow read, not the complete form. Cleanup below
                    # closes only this preview's menu and never chooses a value.
                    await asyncio.wait_for(self._preview_field_options(item, locator, policy),
                                           timeout=min(3, remaining))
                else:
                    await self._preview_field_options(item, locator, policy)
            except asyncio.TimeoutError:
                self._mark_deferred_options(item)
            except Exception:
                item["options"] = item.get("options", [])
            finally:
                # A read may trigger a redirect/expired login. Never close or
                # probe the old field on the replacement document.
                await self._assert_snapshot_document(document_url, phase="after_option_preview")
                # A cancelled preview can leave a portal open. An outer form
                # cancellation must still propagate; never retry a value write.
                if safari_preview:
                    try:
                        await asyncio.wait_for(self._dismiss_options(locator, policy), timeout=2)
                    except asyncio.TimeoutError as exc:
                        raise ValueError("关闭网页选项预览超时；已停止，请在官网关闭下拉菜单后同步") from exc
        _finalize_field_metadata(data)
        _finalize_choice_metadata(data)
        if policy.name == "beisen-italent":
            data = await refine_phoenix_fields(self.page, data)
        data = await refine_autohome_fields(self.page, data)
        data = await refine_ant_resume_fields(self.page, data)
        attachment = await inspect_autohome_attachment(self.page)
        for item in data:
            if item.get("container_key") == "autohome:attachment" and attachment.get("attachment_present"):
                item["current_value"] = attachment["attachment_label"]
        for candidate in await discover_autohome_sections(self.page):
            data.append({**candidate, "name": "autohome-section:" + candidate["id"],
                         "question_text": candidate["label"], "label_source": "explicit",
                         "recognition_confidence": .99, "context": candidate["section"],
                         "current_value": "", "required": False})
        for candidate in await discover_phoenix_sections(self.page):
            data.append({**candidate, "name": "phoenix-section:" + candidate["id"],
                         "question_text": candidate["label"], "label_source": "explicit",
                         "recognition_confidence": .99, "context": candidate["section"],
                         "current_value": "", "required": False})
        _finalize_field_metadata(data)
        fields = enrich_fields([PageField.model_validate(item) for item in data], self.page.url)
        annotate_fields(fields)
        title = await self.page.title()
        await self._assert_snapshot_document(document_url, phase="snapshot_finalization")
        return BrowserSnapshot(
            session_id=self.session_id, url=document_url, title=title,
            browser_engine="safari" if isinstance(self.context, SafariContext) else "chromium",
            recognition_profile=str(recognition_profile["name"]), site_route=site_route, fields=fields,
            extraction_report=build_report(fields, inventory),
        )

    async def _assert_snapshot_document(self, expected_url: str, *, phase: str = "document_check") -> None:
        if isinstance(self.context, SafariContext):
            # Metadata check against the bound window, not the frontmost tab.
            # Test transports may only expose refresh; the production adapter
            # has a cheaper URL-only command that executes no page script.
            if hasattr(self.page, 'read_url'):
                await self.page.read_url()
            else:
                await self.page.refresh()
        if self.page.url != expected_url:
            # Fixed phase names only: never emit URLs, tokens, selectors,
            # question text, browser storage or candidate values to logs.
            logging.getLogger(__name__).warning("snapshot_stopped phase=%s reason=url_changed", phase)
            step = {"site_routing": "识别站点时", "question_capture": "读取题目时",
                    "control_classification": "核对控件类型时", "before_option_preview": "展开选项前",
                    "after_option_preview": "展开选项后", "snapshot_finalization": "完成题目读取时"}.get(phase, "读取控件时")
            raise ValueError(step + '招聘页面已跳转；旧表单已丢弃，没有继续填写。请同步当前页并检查登录状态')

    @staticmethod
    def _mark_deferred_options(item):
        # Empty means unavailable, never a fabricated free-text dropdown.
        item["options"] = []
        item["options_capture"] = "deferred"
        item["help_text"] = (item.get("help_text", "") +
            "；下拉选项尚未读取完成，不代表可随意输入；有确定答案时执行器会重新读取并唯一匹配，"
            "无法匹配则保留给你确认").lstrip("；")

    async def _preview_field_options(self, item, locator, policy):
        if item.get('control_kind') == 'cascade':
            item['cascade_observation'] = await preview_cascade(self.page, locator)
            item['options'] = []  # visible columns are not one flat option list
            item['options_capture'] = 'dependent'
            item['help_text'] = '；'.join(item['cascade_observation']['limitations'])
            return
        if item.get('control_kind') == 'calendar':
            precision = await preview_calendar(self.page, locator)
            item['date_precision'] = precision
            item['options'] = []
            item['help_text'] = ('官网日历要求年月日，不会擅自补日' if precision == 'date' else
                                 '官网日历要求年月' if precision == 'month' else '日历格式尚未核实，不能当普通下拉选择')
            return
        if policy.name == "beisen-italent":
            await self._dismiss_options(locator, policy)
        calendars_before = await calendar_ids(self.page) if policy.name == "beisen-italent" else set()
        before_open = await visible_popup_ids(self.page, policy)
        entries = await scoped_option_entries(self.page, locator, policy)
        if not entries:
            await locator.click(timeout=1800)
            if policy.name == "beisen-italent":
                await self.page.wait_for_timeout(150)
                panel = await calendar_for(locator, self.page, calendars_before)
                precision = await calendar_precision(panel)
                if precision:
                    await locator.evaluate("(el, value) => el.dataset.zhidaDatePrecision = value", precision)
                    item["date_precision"] = precision
                    item["help_text"] = "网页日历要求" + ("年月日" if precision == "date" else "年月") + "；不会用日历单格作为完整日期"
                    item["options"] = []
                    self._option_probe_shapes[item.get("label", "")] = await inspect_component_shapes(self.page)
                    await self._dismiss_options(locator, policy)
                    return
            entries = await self._wait_for_options(locator, policy, before_open)
        item["options"] = list(dict.fromkeys(text for text, _ in entries))
        item["options_capture"] = "observed_subset" if entries else "unavailable"
        if policy.name == "beisen-italent" and entries and any([
                await option.evaluate("el=>el.matches('.area-item-name')") for _, option in entries]):
            item["region_picker"] = True
            await locator.evaluate("el=>el.dataset.zhidaRegionPicker='true'")
            item['region_value_path'] = await read_open_region_path(
                self.page, locator, entries, item.get('current_value', ''))
            self._option_probe_shapes[item.get("label", "")] = await inspect_component_shapes(self.page)
        if policy.name == "beisen-italent" and not entries:
            self._option_probe_shapes[item.get("label", "")] = await inspect_component_shapes(self.page)
        if not isinstance(self.context, SafariContext):
            await self._dismiss_options(locator, policy)

    async def snapshot_for(self, session_id: str) -> BrowserSnapshot:
        self._require(session_id)
        return await self.snapshot()

    async def recognition_diagnostics(self, session_id: str) -> dict:
        result = await inspect_recognition_structure(self._require(session_id))
        result["ant_resume_records"] = await inspect_ant_resume_rejections(self.page)
        result["option_probe_shapes"] = self._option_probe_shapes
        return result

    async def settled_snapshot(self, session_id: str) -> BrowserSnapshot:
        """Observe a bounded, stable form without navigating or writing values.

        Stability only means that the observed structure/options stopped
        changing, not that an unknown ATS has rendered every possible field.
        The known Autohome template additionally requires a rendered school
        record; its stable first shell contains personal fields but is partial.
        """
        page = self._require(session_id)
        initial_url = page.url
        # Safari's Apple Events transport has per-read IPC cost. A five-second
        # wall clock can expire before two observations even of a loaded form.
        # Still bounded: no navigation, reload or value writes during settling.
        deadline = time.monotonic() + (60.0 if isinstance(self.context, SafariContext) else 5.0)
        previous = None
        parsed = urlparse(initial_url)
        autohome = (parsed.scheme == "https" and parsed.netloc.casefold() in {
            "talent.autohome.com.cn", "talent.autohome.com.cn:443"
        } and parsed.path == "/recruit-delivery.html")

        async def bounded(awaitable):
            # wait_for also bounds a slow snapshot/ATS inspector, not only the
            # polling sleeps. No network request, reload or click retry follows.
            try:
                return await asyncio.wait_for(awaitable, timeout=max(.001, deadline - time.monotonic()))
            except asyncio.TimeoutError as exc:
                raise ValueError("申请表仍在加载或选项尚未稳定，请稍后重试；本次未执行填写") from exc

        for attempt in range(8):
            state = await bounded(self.workflow_state(session_id))
            snapshot = await bounded(self.snapshot(probe_options=False))
            latest_state = await bounded(self.workflow_state(session_id))
            # A changed URL or a login/verification page belongs to the caller's
            # stage gate, never to the previous form's fill plan. Clear fields
            # on navigation because a snapshot could straddle two documents.
            if (page.url != initial_url or snapshot.url != initial_url or state.url != initial_url
                    or latest_state.url != initial_url):
                snapshot.fields = []
                return snapshot
            if latest_state.stage not in {"profile_form", "application_form", "review", "unknown"}:
                return snapshot
            fields = [field for field in snapshot.fields if field.field_type != "section-button"]
            personal = any(field.semantic_key.startswith((
                "candidate.", "education.", "experience.", "project.",
            )) for field in fields)
            ready = personal and bool(fields)
            if autohome:
                rendered = await bounded(page.evaluate(r"""() => {
                  const roots = [...document.querySelectorAll('form.validform div[data-bind]')]
                    .filter(el => /name\s*:\s*['\"]eduTemplate['\"]/.test(el.getAttribute('data-bind') || ''));
                  if (roots.length !== 1) return false;
                  return [...roots[0].querySelectorAll('input[data-bind]')].some(el =>
                    /(?:^|,)\s*value\s*:\s*School\s*(?:,|$)/.test(el.getAttribute('data-bind') || '') &&
                    el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden' && !el.disabled);
                }"""))
                ready = ready and rendered and any(
                    field.container_key.startswith("autohome:education:") and
                    field.semantic_key == "education.school" for field in fields)
            signature = (latest_state.stage, tuple(
                (field.field_type, field.name, field.question_text, field.label, field.required,
                 tuple(field.options), field.semantic_key, field.entity_scope, field.container_key)
                for field in snapshot.fields
            ))
            if ready and signature == previous:
                # A third passive read validates stable question ownership.
                # Options are discovered only through an explicit, scoped
                # observation or during execution, never by sweeping the form.
                try:
                    enriched = await asyncio.wait_for(self.snapshot(probe_options=False), timeout=45)
                except asyncio.TimeoutError as exc:
                    raise ValueError("表单已加载，但只读核对题目超时；已停止填写，请同步当前页") from exc
                if page.url != initial_url or enriched.url != initial_url:
                    enriched.fields = []
                else:
                    def structure(sample):
                        return tuple((f.selector, f.field_type, f.name, f.question_text, f.required,
                                      f.section, f.container_key, f.entity_scope) for f in sample.fields)
                    after_probe = await self.workflow_state(session_id)
                    if after_probe.stage not in {"profile_form", "application_form", "review", "unknown"}:
                        enriched.fields = []
                    elif structure(snapshot) != structure(enriched):
                        raise ValueError("只读核对时表单题目或记录归属已变化，已停止旧计划，请重新识别")
                return enriched
            previous = signature if ready else None
            if attempt < 7:
                await bounded(asyncio.sleep(.35))
        raise ValueError("申请表关键栏目或选项尚未稳定，请稍后重试；本次未执行填写")

    async def expandable_sections(self, session_id: str) -> list[dict]:
        """Expose only site-proven, read-only expansion candidates.

        Unknown ATS add buttons deliberately do not enter this automatic
        contract. record_keys use exactly the same keys as snapshot metadata.
        """
        return [item for item in await self.record_inventory(session_id) if item["selector"]]

    async def record_inventory(self, session_id: str) -> list[dict]:
        page = self._require(session_id)
        kinds = {"education": "education", "experience": "internships", "project": "projects"}
        return [{
            "id": item["id"], "selector": item["selector"],
            "kind": kinds[item["semantic_section"]], "record_count": item["record_count"],
            "container_key": item["container_key"], "record_keys": list(item["record_keys"]),
            "label": item["label"],
        } for item in [*await discover_autohome_sections(page), *await inspect_phoenix_sections(page),
                      *await inspect_ant_resume_records(page)]
            if item["semantic_section"] in kinds]

    async def expand_missing_section(self, session_id: str, candidate: dict) -> BrowserSnapshot:
        """Expand once from a fresh exact candidate, without guessing a record."""
        current = next((item for item in await self.expandable_sections(session_id)
                        if item["id"] == candidate.get("id")), None)
        if not current or any(candidate.get(key) != value for key, value in current.items()):
            raise ValueError("新增经历入口或记录数量已变化，请重新识别，避免重复增加")
        return await self.expand_section(session_id, current["selector"], candidate_id=current["id"])

    async def inspect_field(self, session_id: str, selector: str) -> BrowserSnapshot:
        """Bring one collected field into view and retry read-only option discovery."""
        page = self._require(session_id)
        before = await self.snapshot(probe_options=False)
        field = next((item for item in before.fields if item.selector == selector), None)
        if not field:
            raise LookupError("字段已经变化，请重新分析当前页面")
        if field.field_type in {"file", "section-button", "password", "hidden"} or is_declaration(field):
            raise ValueError("这个控件不支持定位读取，请使用对应的上传或展开按钮")
        locator = page.locator(selector).first
        if not await locator.count():
            raise LookupError("字段已经变化，请重新分析当前页面")
        await page.bring_to_front()
        await locator.scroll_into_view_if_needed(timeout=5000)
        await locator.evaluate("""
        el => {
          const oldOutline = el.style.outline;
          const oldOffset = el.style.outlineOffset;
          const oldBackground = el.style.backgroundColor;
          el.style.outline = '3px solid #d99025';
          el.style.outlineOffset = '4px';
          el.style.backgroundColor = 'rgba(255, 243, 205, .45)';
          setTimeout(() => {
            if (!el.isConnected) return;
            el.style.outline = oldOutline;
            el.style.outlineOffset = oldOffset;
            el.style.backgroundColor = oldBackground;
          }, 6000);
        }
        """)
        if field.field_type == "combobox":
            observations = await self.observe_form_controls(session_id, [selector])
            refreshed = await self.snapshot(probe_options=False)
            merge_observed_metadata(refreshed, observations)
        else:
            refreshed = await self.snapshot(probe_options=False)
            refresh_report(refreshed)
        await self._assert_snapshot_document(before.url)
        return refreshed

    async def observe_page_region(self, session_id: str, selector: str, *, include_image: bool = False,
                                  expected_snapshot=None, bring_into_view: bool = False,
                                  audit_only: bool = False):
        """Read an exact collected question, never arbitrary pages or a desktop."""
        from .page_observation import observe_region
        from .form_routing import CREDENTIALS

        page = self._require(session_id)
        before = await self.snapshot(probe_options=False)
        field = next((f for f in before.fields if f.selector == selector), None)
        if not field:
            raise ValueError('只允许观察当前已采集的题目，请先同步页面')
        if expected_snapshot is not None:
            expected = next((f for f in expected_snapshot.fields if f.selector == selector), None)
            if not expected or before.url != expected_snapshot.url or any(
                    getattr(expected,k)!=getattr(field,k) for k in (
                        'field_type','question_text','container_key','control_group_key','current_value',
                        'question_candidates','constraints','required_evidence')):
                raise ValueError('模型持有的题目已变化，未读取新题目，停止旧分析')
        if (field.field_type in {'password','hidden','section-button'}
                or not audit_only and (is_declaration(field) or field.field_type == 'file')
                or CREDENTIALS.search(' '.join((field.label,field.question_text,field.name,field.autocomplete)))):
            raise ValueError('账号、验证码、声明、附件和展开按钮不进入题目观察工具')
        await self._assert_snapshot_document(before.url)
        if bring_into_view:
            # Only the exact scanner-issued, guarded target. No click, input,
            # expansion, or navigation; whole-page audits need offscreen rows.
            await page.locator(selector).scroll_into_view_if_needed(timeout=3000)
            await self._assert_snapshot_document(before.url)
        result = await observe_region(page, selector, include_image=include_image)
        await self._assert_snapshot_document(before.url)
        after = await self.snapshot(probe_options=False)
        fresh = next((f for f in after.fields if f.selector == selector), None)
        if not fresh or after.url != before.url or any(getattr(fresh,k)!=getattr(field,k) for k in (
                'field_type','question_text','container_key','control_group_key','current_value',
                'question_candidates','constraints','required_evidence')):
            raise ValueError('观察期间题目身份或资料发生变化，已丢弃结果，请重新同步')
        return result

    async def observe_form_controls(self, session_id: str, selectors: list[str], *,
                                    expected_snapshot: BrowserSnapshot | None = None,
                                    expected_workflow=None) -> list[PageField]:
        """A bounded model tool: observe ONLY collected, non-declaration fields.

        Opens a menu/calendar, never selects/inputs/submits. The caller owns
        the operation lock; its existing task/owner guards still apply.
        """
        from .form_routing import CREDENTIALS
        from .extraction_audit import document_fingerprint

        if not selectors or len(set(selectors)) != len(selectors) or len(selectors) > 4:
            raise ValueError('每次只允许核对1至4个当前表单控件')
        state = await self.workflow_state(session_id)
        if expected_workflow is not None and execution_identity_changes(expected_workflow, state):
            raise ValueError('打开控件前岗位或表单步骤已变化，停止旧观察')
        if state.stage not in {'profile_form','application_form','review'} or state.navigation_blocker:
            raise ValueError('当前不是可核对的申请表，已停止控件观察')
        snapshot = await self.snapshot(probe_options=False)
        if expected_snapshot is not None and document_fingerprint(expected_snapshot) != document_fingerprint(snapshot):
            raise ValueError('打开控件前题目、记录或填写值已变化，停止旧观察；请重新同步')
        baseline = document_fingerprint(snapshot)
        fields = {field.selector:field for field in snapshot.fields}
        if any(selector not in fields for selector in selectors):
            raise ValueError('只能核对本次已读取的字段，不能使用其他网页或任意选择器')
        requested = [fields[selector] for selector in selectors]
        if any(is_declaration(f) or f.field_type in {'file','password','hidden','section-button'}
                or CREDENTIALS.search(' '.join((f.label,f.question_text,f.name,f.autocomplete)))
                for f in requested):
            raise ValueError('声明、附件、账号或展开操作不进入模型控件工具')
        if any(extraction_block_reason(f) for f in requested):
            raise ValueError('题干、控件类型或记录归属未核实，不能打开选项；请先定向读取原题上下文')
        policy = policy_for(snapshot.site_route.adapter)
        observations = []
        for field in requested:
            # An earlier menu can trigger a reactive form rebuild. Check the
            # whole document again BEFORE clicking the next target, not only
            # at batch end (an old combobox may now be a submit button).
            live_snapshot = await self.snapshot(probe_options=False)
            live_state = await self.workflow_state(session_id)
            if (execution_identity_changes(state, live_state) or live_state.navigation_blocker
                    or baseline != document_fingerprint(live_snapshot)):
                raise ValueError('打开控件前题目、岗位、步骤或填写值已变化，停止旧观察；请重新同步')
            await self._assert_snapshot_document(snapshot.url)
            item = field.model_dump()
            control = self.page.locator(field.selector)
            if field.field_type == 'combobox':
                try:
                    await asyncio.wait_for(self._preview_field_options(item, control, policy), timeout=15)
                except asyncio.TimeoutError:
                    self._mark_deferred_options(item)
                finally:
                    await self._assert_snapshot_document(snapshot.url)
                    await self._dismiss_options(control, policy)
            observations.append(PageField.model_validate(item))
        after = await self.snapshot(probe_options=False)
        after_state = await self.workflow_state(session_id)
        current = {f.selector:f for f in after.fields}
        if execution_identity_changes(state, after_state) or after_state.navigation_blocker:
            raise ValueError('核对控件时网页阶段或地址改变，停止旧计划')
        if baseline != document_fingerprint(after):
            raise ValueError('核对控件时表单题目、记录归属或填写值变化，已丢弃结果；请重新同步')
        for field in requested:
            fresh = current.get(field.selector)
            if fresh is None or any(getattr(fresh,k) != getattr(field,k) for k in (
                'field_type','question_text','container_key','control_group_key','current_value',
                'control_kind','question_candidates','constraints','required_evidence','section_path',
                'entity_scope','record_evidence','required')):
                raise ValueError('核对控件时原题、记录归属或填写值改变，请先同步，不会沿用旧计划')
        by_selector = {field.selector:field for field in observations}
        combined = [by_selector.get(field.selector, field) for field in after.fields]
        annotate_fields(combined)
        current = {field.selector:field for field in combined}
        return [current[selector] for selector in selectors]

    async def expand_section(self, session_id: str, selector: str, candidate_id: str = "") -> BrowserSnapshot:
        page = self._require(session_id)
        snapshot = await self.snapshot()
        field = next((item for item in snapshot.fields if item.selector == selector), None)
        if not field or field.field_type != "section-button":
            raise ValueError("这不是可以安全展开的资料栏目")
        if field.name.startswith("autohome-section:"):
            expected = field.name.removeprefix("autohome-section:")
            if not candidate_id or candidate_id != expected:
                raise ValueError("新增经历入口已变化，请重新识别后再增加，避免重复记录")
            await expand_autohome_section(page, candidate_id)
            return await self.snapshot()
        if field.name.startswith("phoenix-section:"):
            expected = field.name.removeprefix("phoenix-section:")
            if not candidate_id or candidate_id != expected:
                raise ValueError("新增经历入口已变化，请重新识别后再增加")
            await expand_phoenix_section(page, candidate_id)
            return await self.snapshot()
        text = f"{field.section} {field.group_label} {field.label}"
        if not re.search(r"技能|语言|教育|学历|经历|项目|证书|奖项|skill|language|education|experience|project|certificate|award", text, re.I):
            raise ValueError("只允许展开教育、经历、项目、技能、语言、证书或奖项栏目")
        locator = page.locator(selector).first
        if not await locator.count():
            raise LookupError("栏目按钮已经变化，请重新分析页面")
        await locator.click(timeout=8000)
        await page.wait_for_timeout(500)
        return await self.snapshot()

    async def import_resume_with_site_parser(self, session_id: str,
                                             resume_path: Path, confirm_site_parse: bool = False) -> NativeResumeImportResult:
        """Upload a resume and let the ATS parser produce a first draft.

        Only narrowly named resume-parse/import controls may be clicked. Generic
        confirm/next/save/apply buttons remain a manual gate.
        """
        page = self._require(session_id)
        workflow = await self.workflow_state(session_id)
        if workflow.navigation_blocker:
            raise ValueError(workflow.navigation_blocker)
        if workflow.stage in {"homepage", "job_list", "job_detail", "auth_required", "registration_required", "verification_required"}:
            raise ValueError("请先进入具体岗位的申请表，不能在招聘导航页上传简历")
        before = await self.snapshot()
        attachment_before = await inspect_autohome_attachment(page)
        if attachment_before.get("blocking_dialog"):
            raise ValueError("招聘网页仍有待处理弹窗，请先完成或取消当前操作，再上传简历")
        resume_fields = [field for field in before.fields if field.field_type == "file" and (
            any(hint in f"{field.label} {field.name}".lower()
                for hint in ("resume", "cv", "curriculum", "简历"))
            or any(hint in field.accept.lower() for hint in ("pdf", "doc", "word"))
        )]
        if not resume_fields:
            raise ValueError("当前页面没有识别到简历上传控件，请先进入在线简历或申请表页面")
        upload = page.locator(resume_fields[0].selector).first
        await upload.set_input_files(str(resume_path), timeout=15000)
        uploaded = await upload.evaluate(
            "el => [...(el.files || [])].map(file => file.name).join(', ')"
        )
        if resume_path.name not in uploaded and not attachment_before.get("supported"):
            raise RuntimeError("简历已选择，但招聘网页没有保留该文件")

        await page.wait_for_timeout(1800)
        attachment = await inspect_autohome_attachment(page, attachment_before)
        if attachment.get("supported"):
            for _ in range(240):
                if attachment.get("error") or attachment.get("upload_verified"):
                    break
                await page.wait_for_timeout(500)
                attachment = await inspect_autohome_attachment(page, attachment_before)
            if attachment.get("error"):
                raise ValueError("招聘网站拒绝简历上传：" + attachment["error"])
            if not attachment.get("upload_verified"):
                raise ValueError("等待120秒仍未观察到招聘网站接收新附件的证据，请先核对官网处理状态，不要立即重复上传")
            if attachment.get("requires_parse_confirmation") and confirm_site_parse:
                await confirm_autohome_resume_parse(page)
                await page.wait_for_timeout(500)
        after = await self.snapshot()
        changed = _changed_field_count(before, after)
        trigger_clicked = False
        trigger_label = ""
        if attachment.get("requires_parse_confirmation"):
            if not confirm_site_parse:
                return NativeResumeImportResult(snapshot=after, uploaded_file=resume_path.name,
                    status="needs_user_action", changed_fields=changed,
                    message="官网已接收附件，正等待确认是否用简历解析结果覆盖个人信息；尚未完成解析填写")
            trigger_clicked = True
            trigger_label = "是否变更个人信息？—确定（仅导入解析结果）"
        if changed == 0 and not trigger_clicked:
            controls = page.locator('button, [role="button"], input[type="button"]')
            for index in range(min(await controls.count(), 200)):
                control = controls.nth(index)
                try:
                    if not await control.is_visible() or await control.is_disabled():
                        continue
                    label = re.sub(r"\s+", "", (
                        await control.inner_text() or await control.get_attribute("value") or
                        await control.get_attribute("aria-label") or ""
                    ).strip())
                    if not SAFE_RESUME_PARSE_LABEL.fullmatch(label):
                        continue
                    await control.click(timeout=8000)
                    trigger_clicked = True
                    trigger_label = label
                    break
                except Exception:
                    continue
            if trigger_clicked:
                await page.wait_for_timeout(4500)
                after = await self.snapshot()
                changed = _changed_field_count(before, after)

        if changed:
            status = "parsed"
            message = f"招聘网站已根据简历更新 {changed} 个字段；请用智达核对网站识别结果"
        elif trigger_clicked:
            status = "needs_user_action"
            message = "已触发招聘网站的简历解析，但尚未观察到字段变化；请查看网页是否要求确认或选择解析范围"
        else:
            status = "uploaded"
            message = "简历已上传；如果网页要求手动确认解析，请在浏览器完成后点击“核对网站填写结果”"
        return NativeResumeImportResult(
            snapshot=after, uploaded_file=resume_path.name, trigger_clicked=trigger_clicked,
            trigger_label=trigger_label, changed_fields=changed, status=status, message=message,
        )

    async def pre_submit_check(self, session_id: str) -> PreSubmitCheck:
        page = self._require(session_id)
        policy = policy_for((await route_for_page(page)).adapter)
        data = await page.evaluate("""
        profile => {
          const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
          const customWrapperSelector = [
            ...(profile?.control_selectors || []),
            '.ant-select-selector', '.ant-select-selection', '.ant-calendar-picker', '.ant-picker', '.ant-cascader-picker', '.ant-cascader', '.arco-select-view', '.el-select__wrapper', '.ivu-select-selection',
            '.semi-select', '.t-select__wrap', '[class*="select-selector"]',
            '[class*="select__selector"]', '[class*="select-view"]',
            '[class*="cascader-picker"]', '[class*="picker-input"]'
          ].join(', ');
          const customSelector = '[role="combobox"], [aria-haspopup="listbox"], ' + customWrapperSelector;
          const labelFor = el => {
            const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
            const question = el.closest('[data-zhida-choice-group], .application-question, .application-additional, fieldset, [role="radiogroup"], [role="group"], .form-field, .field, .ant-form-item, .arco-form-item, .el-form-item, [class*="form-item"], [class*="formItem"]');
            return clean(question?.getAttribute('data-zhida-choice-label') || explicit?.innerText || question?.querySelector('.application-label, legend, .question-label')?.innerText ||
              el.closest('label')?.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name);
          };
          const canonical = el => el.closest('.ant-calendar-picker, .ant-picker, .ant-cascader-picker, .ant-cascader') ||
            el.closest(customWrapperSelector) || (el.matches(customSelector) ? el : (el.closest(customSelector) || el));
          const candidates = [...new Set([...document.querySelectorAll(
            'input, select, textarea, [role="radio"], [role="checkbox"], ' + customSelector
          )].map(canonical))].filter(el => {
            const role = (el.getAttribute('role') || '').toLowerCase();
            const customSelect = role === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox' || el.matches(customSelector);
            const type = (el.type || '').toLowerCase();
            const optionInput = ['radio', 'checkbox'].includes(type) || ['radio', 'checkbox'].includes(role);
            if (!customSelect && !optionInput && ['hidden','submit','button','image','reset','password'].includes(type)) return false;
            const optionVisible = optionInput && (el.closest('label')?.getClientRects().length || el.parentElement?.getClientRects().length);
            return !el.disabled && (type === 'file' || el.getClientRects().length > 0 || optionVisible);
          });
          let filledCount = 0;
          let requiredTotal = 0;
          const missing = [];
          const handledRadioNames = new Set();
          for (const el of candidates) {
            const marker = el.getAttribute('data-zhida-field') || '';
            const label = labelFor(el).slice(0, 500);
            const role = (el.getAttribute('role') || '').toLowerCase();
            const fieldType = ['radio', 'checkbox'].includes(role) ? role : (el.type || el.tagName).toLowerCase();
            const required = el.required || el.querySelector('input')?.required || el.getAttribute('aria-required') === 'true' || /[*✱]/.test(label);
            let filled = false;
            const customSelect = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox' || el.matches(customSelector);
            if (el.type === 'file') filled = Boolean(el.files?.length);
            else if (fieldType === 'radio') {
              const group = el.closest('[data-zhida-choice-group], [role="radiogroup"], fieldset, [role="group"]');
              const groupKey = group?.getAttribute('data-zhida-choice-group') || el.name ||
                ('radio-' + [...document.querySelectorAll('[role="radiogroup"], fieldset, [role="group"]')].indexOf(group));
              if (handledRadioNames.has(groupKey)) continue;
              handledRadioNames.add(groupKey);
              const members = group?.hasAttribute('data-zhida-choice-group') ?
                [...group.querySelectorAll('input[type="radio"], [role="radio"]')] : el.name ? [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)] :
                [...(group?.querySelectorAll('input[type="radio"], [role="radio"]') || [el])];
              filled = members.some(item => item.checked || item.getAttribute('aria-checked') === 'true');
            } else if (fieldType === 'checkbox') {
              filled = el.checked || el.getAttribute('aria-checked') === 'true';
            } else if (customSelect) {
              const selected = [...el.querySelectorAll('[class*="selection-item"], [class*="selected-value"], [class*="selected-item"]')]
                .map(item => clean(item.innerText || item.textContent)).filter(Boolean);
              const value = selected.join(', ') || clean(el.value || el.getAttribute('aria-valuetext') || el.querySelector('input')?.value || el.innerText);
              filled = Boolean(value) && !/^(select|choose|请选择|请选择一项|暂未选择)[.\\s…]*$/i.test(value);
            }
            else filled = clean(el.value).length > 0;
            if (filled) filledCount += 1;
            if (required) {
              requiredTotal += 1;
              if (!filled) missing.push({selector: marker ? `[data-zhida-field="${marker}"]` : '', label, field_type: fieldType});
            }
          }
          const validationErrors = [...document.querySelectorAll('[aria-invalid="true"], .error-message, .field-error, .application-error, .Validform_wrong, #file-err-msg')]
            .filter(el => el.getClientRects().length > 0).map(el => clean(el.innerText)).filter(Boolean).slice(0, 30);
          const humanChallenges = [];
          const challengePresent = document.querySelector(
            '.h-captcha, .g-recaptcha, [data-sitekey], iframe[src*="captcha" i], [class*="turnstile" i]');
          const challengeCompleted = [...document.querySelectorAll(
            'textarea[name="g-recaptcha-response"], textarea[name="h-captcha-response"], input[name="cf-turnstile-response"]')]
            .some(el => clean(el.value).length > 0);
          if (challengePresent && !challengeCompleted) {
            humanChallenges.push('页面包含需要用户完成的人机验证');
          }
          const fileUploads = [...document.querySelectorAll('input[type="file"]')]
            .flatMap(el => [...(el.files || [])].map(file => file.name));
          const submitLabels = [...document.querySelectorAll('button, input[type="submit"]')]
            .filter(el => el.getClientRects().length > 0)
            .map(el => ({text: clean(el.innerText || el.value || el.getAttribute('aria-label')),
              primary: el.type === 'submit' || /submit/i.test(`${el.id} ${el.getAttribute('data-qa') || ''}`)}))
            .filter(item => /submit|apply|send application|提交|申请/i.test(item.text))
            .sort((a, b) => Number(b.primary) - Number(a.primary)).map(item => item.text).slice(0, 10);
          return {field_count: candidates.length, required_total: requiredTotal, filled_count: filledCount, required_missing: missing,
            validation_errors: [...new Set(validationErrors)], human_challenges: humanChallenges,
            file_uploads: fileUploads, submit_labels: submitLabels};
        }
        """, policy.field_profile())
        missing_rows = data["required_missing"]
        parsed = urlparse(page.url)
        phoenix_form = policy.name == "beisen-italent" and await page.locator('.form-item.form-item--phoenix').count() > 0
        if phoenix_form or (parsed.hostname == "talent.autohome.com.cn" and parsed.path == "/recruit-delivery.html"):
            # Share the verified field metadata with planning; the legacy
            # DOM check misses Validform datatype and counts Select2 twice.
            observed = await self.snapshot(probe_options=False)
            attachment = await inspect_autohome_attachment(page)
            if attachment.get("attachment_present"):
                data["file_uploads"] = [attachment["attachment_label"]]
            if attachment.get("error"):
                data["validation_errors"].append(attachment["error"])
            if attachment.get("blocking_dialog"):
                data["human_challenges"].append("官网仍有待处理弹窗，尚未完成核对")
            def filled(item):
                value = item.current_value.strip()
                if item.field_type in {"checkbox", "radio"}:
                    return value.lower() == "true"
                return bool(value) and not re.fullmatch(r"请选择(?:一项)?|暂未选择", value)
            logical_fields = []
            grouped = set()
            for item in observed.fields:
                if item.field_type == "radio" and item.control_group_key:
                    if item.control_group_key in grouped:
                        continue
                    grouped.add(item.control_group_key)
                    members = [peer for peer in observed.fields if peer.field_type == "radio" and
                               peer.control_group_key == item.control_group_key]
                    item = item.model_copy(update={"required": any(peer.required for peer in members),
                        "current_value": "true" if any(filled(peer) for peer in members) else "false"})
                logical_fields.append(item)
            missing_rows = [dict(selector=item.selector, label=item.question_text or item.label,
                                 field_type=item.field_type) for item in logical_fields
                            if item.required and not filled(item)]
            data["required_total"] = sum(item.required for item in logical_fields)
            data["filled_count"] = sum(filled(item) for item in logical_fields)
            data["field_count"] = len(logical_fields)
        _finalize_field_metadata(missing_rows)
        missing = [RequiredFieldIssue.model_validate(item) for item in missing_rows]
        workflow = await self.workflow_state(session_id)
        if (parsed.hostname == "talent.autohome.com.cn" and parsed.path == "/recruit-delivery.html"
                and workflow.final_submit_present and "提交简历" not in data["submit_labels"]):
            data["submit_labels"].append("提交简历")
        recognized_form = data["field_count"] > 0 and workflow.stage in {"profile_form", "application_form", "review"}
        return PreSubmitCheck(url=page.url, ready=recognized_form and not missing and not data["validation_errors"] and not data["human_challenges"],
                              required_total=data["required_total"], filled_count=data["filled_count"],
                              required_missing=missing, validation_errors=data["validation_errors"],
                              human_challenges=data["human_challenges"],
                              file_uploads=data["file_uploads"], submit_labels=data["submit_labels"])

    async def _resolve_field(self, field: PageField):
        if not self.page:
            raise LookupError("浏览器页面已关闭")
        locator = self.page.locator(field.selector).first
        if await locator.count():
            return field, locator
        fresh = await self.snapshot()
        candidates = [item for item in fresh.fields if item.field_type == field.field_type]
        if field.name:
            named = [item for item in candidates if item.name == field.name]
            if len(named) == 1:
                replacement = named[0]
                return replacement, self.page.locator(replacement.selector).first
        identity = _normalized_option(" ".join(filter(None, (field.section, field.group_label, field.label))))
        matches = [item for item in candidates if _normalized_option(
            " ".join(filter(None, (item.section, item.group_label, item.label)))
        ) == identity]
        if len(matches) != 1:
            raise LookupError(f"网页更新后无法唯一定位字段：{field.group_label or field.label or field.name}")
        replacement = matches[0]
        return replacement, self.page.locator(replacement.selector).first

    async def _wait_for_options(self, control, policy, before_open, wanted: str = ""):
        # Poll only the current control's menu, bounded by the selected ATS policy.
        entries = []
        deadline = time.monotonic() + policy.option_wait_ms / 1000
        while True:
            entries = await scoped_option_entries(self.page, control, policy, before_open)
            if entries and (not wanted or _best_option(wanted, [text for text, _ in entries])):
                return entries
            if time.monotonic() >= deadline:
                break
            await self.page.wait_for_timeout(100)
        return entries

    async def _dismiss_options(self, control, policy) -> None:
        await self.page.keyboard.press("Escape")
        if policy.name != "beisen-italent":
            return
        # Phoenix does not consistently close a portalled menu on Escape.
        # Click only this control's own noninteractive title (never another
        # input, consent, navigation or submission control).
        caption = await control.evaluate_handle("""el => {
          const q=el.closest('.form-item.form-item--phoenix');
          const title=q?.querySelector(':scope > .form-item__title');
          return title&&!title.querySelector('input,select,textarea,button,a[href],[role="button"]')?title:null;
        }""")
        try:
            node = caption.as_element()
            if node:
                # Deep Phoenix region panels can cover the owning caption.
                # Dispatch only an outside-click to this verified, noninteractive
                # caption; never force a coordinate click through the overlay.
                await node.evaluate("""el=>{
                  el.dispatchEvent(new MouseEvent('mousedown',{bubbles:true}));
                  el.click();
                }""")
        finally:
            await caption.dispose()

    async def _visible_option_entries(self, control):
        if not self.page:
            return []
        policy = policy_for((await route_for_page(self.page)).adapter)
        return await scoped_option_entries(self.page, control, policy)

    async def _select_custom(self, field: PageField, values: list[str], *,
                             before_write: Callable[[], Awaitable[None]] | None = None,
                             on_stage: Callable[[str], None] | None = None) -> list[str]:
        selected: list[str] = []
        def stage(name):
            if on_stage:
                on_stage(name)
        policy = policy_for((await route_for_page(self.page)).adapter)
        if field.control_kind == 'calendar' and not field.date_precision:
            # Passive snapshots deliberately do not open every menu. Probe
            # this one owned calendar just before execution, never infer its
            # precision from a date-like caption or from the requested value.
            stage('date_precision')
            _, calendar_control = await self._resolve_field(field)
            if before_write:
                await before_write()
            field.date_precision = await preview_calendar(self.page, calendar_control)
        if field.date_precision:
            if len(values) != 1:
                raise ValueError("日期控件来源或目标不明确，已停止填写")
            _, control = await self._resolve_field(field)
            await self._dismiss_options(control, policy)
            stage('select_option')
            if field.control_kind == 'calendar':
                value = await select_ant_calendar(self.page, control, values[0], field.date_precision, before_write)
            elif policy.name == 'beisen-italent':
                value = await select_calendar_date(self.page, control, values[0], field.date_precision, before_write)
            else:
                raise ValueError('尚无可核验的日历执行器，不会当普通输入框填写')
            await self._dismiss_options(control, policy)
            return [value]
        if field.control_kind == 'calendar':
            raise ValueError('日历格式尚未核实，已停止；不会在日期框搜索选项')
        if field.control_kind == 'cascade':
            stage('open_popup')
            if len(values) != 1:
                raise ValueError('级联控件需要一个明确的完整路径')
            _, control = await self._resolve_field(field)
            return await select_ant_cascade(self.page, control, values[0], _best_option, before_write)
        if field.region_picker and policy.name == 'beisen-italent':
            if len(values) != 1:
                raise ValueError('地区只能填写一个明确的行政区路径')
            _, control = await self._resolve_field(field)
            await self._dismiss_options(control, policy)
            return await select_region(self.page, control, values[0], policy, _best_option, before_write)
        targets = values if field.multiple else values[:1]
        for wanted in targets:
            live_field, control = await self._resolve_field(field)
            if before_write:
                await before_write()
            await control.scroll_into_view_if_needed(timeout=3000)
            if policy.name == "beisen-italent" and await control.evaluate("el=>el.matches('.phoenix-select')"):
                await self._dismiss_options(control, policy)
            stage('popup_check')
            before_open = await visible_popup_ids(self.page, policy)
            entries = await scoped_option_entries(self.page, control, policy)
            if not entries:
                stage('open_popup')
                if before_write:
                    await before_write()
                await control.click(timeout=8000)
                stage('read_options')
                entries = await self._wait_for_options(control, policy, before_open, wanted)
            match = _best_option(wanted, [text for text, _ in entries])
            if not match:
                search = control if await control.evaluate("el => el.tagName === 'INPUT'") else control.locator("input").first
                if await search.count() and await search.is_editable():
                    stage('search_options')
                    if before_write:
                        await before_write()
                    if isinstance(self.context, SafariContext):
                        await search.fill(wanted, keep_focus=True)
                    else:
                        await search.fill(wanted)
                    entries = await self._wait_for_options(control, policy, before_open, wanted)
                    match = _best_option(wanted, [text for text, _ in entries])
            if not match:
                await self.page.keyboard.press("Escape")
                choices = "、".join(text for text, _ in entries[:12]) or "未读取到选项"
                raise ValueError(f"网页选项中找不到“{wanted}”；当前选项：{choices}")
            matches = [option for text, option in entries if text == match]
            if len(matches) != 1:
                await self.page.keyboard.press("Escape")
                raise ValueError(f"“{wanted}”对应多个网页选项，请在网页中核对层级后选择")
            option = matches[0]
            stage('select_option')
            if before_write:
                await before_write()
            # In the observed region picker, clicking the row text enters the
            # next geographic level. Its explicit radio icon commits that region.
            radio_icon = option.locator('.icon-container.visible').filter(
                has=self.page.locator('svg.area-icon-RadioUnchecked,svg.area-icon-RadioChecked'))
            if (policy.name == 'beisen-italent'
                    and await option.evaluate("el=>el.matches('.area-item-name')")
                    and await radio_icon.count() == 1 and await radio_icon.is_visible()):
                await radio_icon.click(timeout=8000)
            else:
                await option.click(timeout=8000)
            selected.append(match)
            await self.page.wait_for_timeout(250)
            field = live_field
        return selected

    async def _select_native(self, field: PageField, values: list[str], *,
                             before_write: Callable[[], Awaitable[None]] | None = None) -> list[str]:
        _, control = await self._resolve_field(field)
        options = await control.locator("option").evaluate_all(
            "items => items.filter(item => !item.disabled && !item.parentElement?.disabled).map(item => ({label: String(item.textContent || '').trim(), value: item.value}))"
        )
        selected: list[str] = []
        targets = values if field.multiple else values[:1]
        for wanted in targets:
            match = _best_option(wanted, [item["label"] for item in options])
            if match:
                selected.append(match)
                continue
            value_match = _best_option(wanted, [item["value"] for item in options])
            if value_match:
                selected_label = next(item["label"] for item in options if item["value"] == value_match)
                if sum(_option_identity(item["label"]) == _option_identity(selected_label)
                       for item in options) != 1:
                    raise ValueError(f"网页中存在多个同名选项“{selected_label}”，请人工确认")
                selected.append(selected_label)
                continue
            raise ValueError(f"网页下拉选项中找不到“{wanted}”")
        if before_write:
            await before_write()
        await control.select_option(label=selected if field.multiple else selected[0], timeout=8000)
        return selected

    async def _read_field_value(self, field: PageField) -> str:
        live_field, control = await self._resolve_field(field)
        if live_field.field_type in {"checkbox", "radio"}:
            return await control.evaluate(CHOICE_STATE_JS)
        if live_field.field_type in {"select-one", "select-multiple"}:
            selected = [item.strip() for item in await control.locator("option:checked").all_text_contents()]
            return ", ".join(item for item in selected if item) or await control.input_value()
        if live_field.field_type == "combobox":
            if await control.evaluate("el=>el.matches('.phoenix-select')"):
                actual = await control.evaluate(PHOENIX_SELECT_VALUE_JS)
                if live_field.region_picker and actual:
                    audit = await audited_region_path(control, actual)
                    if audit:
                        return audit
                    policy = policy_for((await route_for_page(self.page)).adapter)
                    await self._dismiss_options(control, policy)
                    before = await visible_popup_ids(self.page, policy)
                    await control.click(timeout=3000)
                    entries = await self._wait_for_options(control, policy, before)
                    try:
                        path = await read_open_region_path(self.page, control, entries, actual)
                        return path or actual
                    finally:
                        await self._dismiss_options(control, policy)
                return actual
            return await control.evaluate("""el => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
              const selected = [...el.querySelectorAll('[class*="selection-item"], [class*="selected-value"], [class*="selected-item"], .ant-select-selection-selected-value')]
                .map(item => clean(item.innerText || item.textContent)).filter(Boolean);
              return selected.join(', ') || clean(el.getAttribute('aria-valuetext') || el.querySelector('input')?.value || el.innerText);
            }""")
        return await control.input_value()

    async def execute(self, session_id: str, request: ExecutePlanRequest,
                      resume_path: Path | None = None, *,
                      resume_filename: str = '',
                      before_action: Callable[[], None] | None = None,
                      on_progress: Callable[[int, str], None] | None = None) -> ExecutionResult:
        page = self._require(session_id)
        document = await page.evaluate_handle('() => document')
        try:
            return await self._execute_bound_document(session_id, request, resume_path,
                resume_filename=resume_filename, before_action=before_action,
                document=document, on_progress=on_progress)
        finally:
            try:
                await document.dispose()
            except Exception:
                pass  # A navigation already destroyed this document's handle.

    async def _execute_bound_document(self, session_id: str, request: ExecutePlanRequest,
                      resume_path: Path | None = None, *, resume_filename: str = '',
                      before_action: Callable[[], None] | None = None, document,
                      on_progress: Callable[[int, str], None] | None = None) -> ExecutionResult:
        page = self._require(session_id)
        workflow = await self.workflow_state(session_id)
        if workflow.navigation_blocker:
            raise ValueError(workflow.navigation_blocker)
        if workflow.stage in {"homepage", "job_list", "job_detail", "auth_required", "registration_required", "verification_required"}:
            raise ValueError("当前不是可填写的申请表；不会向首页、岗位筛选或登录控件写入个人资料")
        # A real reload can reuse both URL and selectors. Compare the actual
        # Document object instead of a privacy-rounded numeric timeOrigin.
        results: list[ActionResult] = []
        current_label = ""
        current_phase = "target_check"
        action_index = 0

        def stage(name):
            nonlocal current_phase
            current_phase = name
            if on_progress:
                on_progress(action_index, name)

        def interrupted(reason):
            return _ExecutionTargetChanged(
                f"填写安全检查停止：{reason}；已停止旧填写计划，请重新识别当前页面后再继续",
                results=results, current_label=current_label, current_phase=current_phase)

        async def ensure_current_page() -> None:
            if self._require(session_id) is not page:
                raise interrupted("连接的招聘窗口已变化")
            current = await self.workflow_state(session_id)
            if current.navigation_blocker:
                raise interrupted("当前页面的目标或导航检查未通过")
            changed = execution_identity_changes(workflow, current)
            if page.url != workflow.url and "网页地址" not in changed:
                changed.append("网页地址")
            if changed:
                raise interrupted("、".join(changed) + "变化")
            try:
                same_document = await document.evaluate('original => original === document')
            except Exception as exc:
                raise interrupted("网页文档已重新加载或无法核对原文档") from exc
            if not same_document:
                raise interrupted("网页文档已重新加载（即使地址未变化）")
            if self.page is not page:
                raise interrupted("连接的招聘窗口已变化")

        snapshot = await self.snapshot()
        fields = {field.selector: field for field in snapshot.fields}
        native_identities = await page.evaluate(NATIVE_WRITE_IDENTITIES, list(fields))
        execution_policy = policy_for(workflow.adapter)

        async def ensure_native_field(original: PageField, current: PageField) -> None:
            expected = native_identities.get(original.selector)
            if expected is None or original.field_type == "combobox":
                return
            metadata = ("name", "field_type", "label", "section", "group_label", "container_key", "entity_scope")
            live = await page.evaluate(NATIVE_WRITE_IDENTITIES, [current.selector])
            if (live.get(current.selector) != expected or any(
                    getattr(original, key) != getattr(current, key) for key in metadata)):
                raise interrupted("字段题干、类型或所属记录变化")

        applied_selectors: set[str] = set()
        if before_action:
            before_action()
        if resume_path:
            resume_fields = [field for field in snapshot.fields if field.field_type == "file" and
                             any(hint in f"{field.label} {field.name}".lower()
                                 for hint in ("resume", "cv", "curriculum", "简历"))]
            for field in resume_fields[:1]:
                current_label = field.label or "Resume/CV"
                try:
                    await ensure_current_page()
                    await ensure_native_field(field, field)
                    if before_action:
                        before_action()
                    name=Path(resume_filename.replace('\\','/')).name if resume_filename else resume_path.name
                    if not name or Path(name).suffix.casefold()!=resume_path.suffix.casefold():
                        raise ValueError('简历原始文件名与所选原件格式不一致，未上传')
                    upload=({'name':name,'mimeType':mimetypes.guess_type(name)[0] or 'application/octet-stream',
                             'buffer':resume_path.read_bytes()} if resume_filename else str(resume_path))
                    await page.locator(field.selector).first.set_input_files(upload, timeout=10000)
                    actual = await page.locator(field.selector).first.evaluate(
                        "el => [...(el.files || [])].map(file => file.name).join(', ')")
                    verified = actual == name
                    results.append(ActionResult(selector=field.selector, label=field.label or "Resume/CV",
                                                status="filled" if verified else "failed", verified=verified,
                                                actual_value=actual, message="附件选择已回读核验；尚不代表官网接收或申请提交" if verified else "简历上传后未能验证"))
                except _ExecutionTargetChanged:
                    raise
                except Exception as exc:
                    results.append(ActionResult(selector=field.selector, label=field.label or "Resume/CV",
                                                status="failed", message=str(exc)[:240]))
                current_label = ""
        for action_index, action in enumerate(request.actions, 1):
            current_label = action.label
            # Profile updates can arrive through a different API while this
            # batch awaits browser I/O. Stop before the next old-value write.
            stage('target_check')
            await ensure_current_page()
            if before_action:
                before_action()
            field = fields.get(action.selector)
            field_description = " ".join(filter(None, (
                field.section, field.group_label, field.label, field.name
            ))).lower() if field else ""
            deterministically_sensitive = any(hint in field_description for hint in SENSITIVE_FIELD_HINTS)
            deterministically_manual = any(hint in field_description for hint in MANUAL_CONFIRM_FIELD_HINTS)
            legal_acknowledgement = bool(field and is_declaration(field))
            observation_blocker = extraction_block_reason(field) if field else ''
            subject_requires_confirmation = bool(field and (
                family_subject(field) or formal_employment_only(field)))
            compatible = bool(field) and (
                (action.action == "fill" and field.field_type not in {"checkbox", "radio", "select-one", "select-multiple", "combobox"})
                or (action.action == "select" and field.field_type in {"select-one", "select-multiple", "combobox"})
                or (action.action == "check" and field.field_type in {"checkbox", "radio"})
            )
            unsafe = (
                field is None
                or bool(observation_blocker)
                or legal_acknowledgement
                or action.action not in {"fill", "select", "check"}
                or not compatible
                or (action.action in {"fill", "select"} and not str(action.value).strip())
                or (action.sensitive and not action.user_confirmed)
                or (deterministically_sensitive and not action.user_confirmed)
                or (deterministically_manual and not action.user_confirmed)
                or (subject_requires_confirmation and not action.user_confirmed)
                or action.confidence < request.min_confidence
            )
            if unsafe:
                results.append(ActionResult(selector=action.selector, label=action.label, status="skipped",
                                            message=observation_blocker or ("声明、承诺或协议必须由本人在招聘网页阅读并确认" if legal_acknowledgement
                                            else "家属资料需要本人明确确认，不能直接使用候选人的姓名、学历或联系方式" if field and family_subject(field)
                                            else "官网只接受正式工作经历，不能直接使用实习；请确认真实正式工作情况" if field and formal_employment_only(field)
                                            else "需要确认、敏感或置信度不足")))
                current_label = ""
                continue
            try:
                stage('resolve')
                field, locator = await self._resolve_field(field)
                if execution_policy.name == 'beisen-italent':
                    await self._dismiss_options(locator, execution_policy)

                async def ensure_write_target() -> None:
                    await ensure_current_page()
                    await ensure_native_field(fields[action.selector], field)

                await ensure_write_target()
                if before_action:
                    before_action()
                expected_values = ([str(action.value)] if field.region_picker else _split_values(action.value))
                stage('write_value')
                if action.action == "fill":
                    await locator.fill(str(action.value), timeout=8000)
                elif action.action == "select":
                    if field.field_type == "combobox":
                        await self._select_custom(field, expected_values[:20], before_write=ensure_write_target,
                                                  on_stage=stage)
                    else:
                        await self._select_native(field, expected_values, before_write=ensure_write_target)
                elif action.action == "check":
                    native_check = await locator.evaluate("el => el.tagName === 'INPUT'")
                    if native_check:
                        try:
                            await ensure_write_target()
                            await locator.set_checked(bool(action.value), timeout=8000)
                        except Exception:
                            await ensure_write_target()
                            await locator.evaluate("""(el, checked) => {
                              el.checked = checked;
                              el.dispatchEvent(new Event('input', {bubbles: true}));
                              el.dispatchEvent(new Event('change', {bubbles: true}));
                            }""", bool(action.value))
                    else:
                        state = await locator.evaluate(CHOICE_STATE_JS)
                        if state not in {"true", "false"}:
                            raise ValueError("无法核实自定义选项的选中状态，已停止填写，请在网页中确认")
                        checked = state == "true"
                        if checked != bool(action.value):
                            await ensure_write_target()
                            await locator.click(timeout=8000)
                applied_selectors.add(action.selector)
                stage('read_value')
                await ensure_native_field(fields[action.selector], field)
                actual = await self._read_field_value(field)
                if field.field_type in {"checkbox", "radio"}:
                    expected = str(bool(action.value)).lower()
                    verified = actual.strip() == expected.strip()
                elif field.field_type in {"select-one", "select-multiple", "combobox"}:
                    expected = str(action.value)
                    verified = (region_values_match(expected, actual) if field.region_picker
                                else _selected_values_match(expected_values, actual))
                else:
                    expected = str(action.value)
                    verified = actual.strip() == expected.strip()
                results.append(ActionResult(selector=action.selector, label=action.label,
                                            status="filled" if verified else "failed", verified=verified,
                                            actual_value=actual,
                                            message="回读验证成功" if verified else f"回读值不一致，期望 {expected}"))
            except _ExecutionTargetChanged:
                raise
            except Exception as exc:
                results.append(ActionResult(selector=action.selector, label=action.label, status="failed",
                                            message=str(exc)[:240]))
            current_label = ""
        if before_action:
            before_action()
        stage('settle')
        await page.wait_for_timeout(800)
        await ensure_current_page()
        # A reactive ATS may normalize or clear a value after the input event.
        # Re-read every planned action after the page settles so the result is
        # based on final DOM state instead of an optimistic immediate read.
        actions_by_selector = {action.selector: action for action in request.actions}
        for result in results:
            current_label = result.label
            stage('final_verify')
            action = actions_by_selector.get(result.selector)
            field = fields.get(result.selector)
            if (result.status not in {"filled", "failed"} or result.selector not in applied_selectors
                    or not action or not field):
                continue
            try:
                await ensure_native_field(field, field)
                expected = str(action.value)
                if field.field_type in {"checkbox", "radio"}:
                    result.actual_value = await self._read_field_value(field)
                    result.verified = result.actual_value == str(bool(action.value)).lower()
                elif field.field_type in {"select-one", "select-multiple", "combobox"}:
                    result.actual_value = await self._read_field_value(field)
                    result.verified = (region_values_match(str(action.value), result.actual_value)
                                       if field.region_picker else
                                       _selected_values_match(_split_values(action.value), result.actual_value))
                else:
                    result.actual_value = await self._read_field_value(field)
                    result.verified = result.actual_value.strip() == expected.strip()
                if not result.verified:
                    result.status = "failed"
                    result.message = f"页面稳定后回读值不一致，期望 {expected}"
                else:
                    # React selection state and Phoenix radio animations may
                    # commit after the first read. Only completed operations
                    # are eligible for this settled-state verification; a click
                    # exception cannot be reclassified as a successful write.
                    result.status = "filled"
                    result.message = "页面稳定后回读验证成功"
            except _ExecutionTargetChanged:
                raise
            except Exception as exc:
                result.status = "failed"
                result.verified = False
                result.message = str(exc)[:240]
        current_label = ""
        check = await self.pre_submit_check(session_id)
        return ExecutionResult(url=page.url, completed=sum(r.status == "filled" for r in results),
                               skipped=sum(r.status == "skipped" for r in results),
                               failed=sum(r.status == "failed" for r in results),
                               verified=sum(r.verified for r in results),
                               unverified=sum(r.status == "filled" and not r.verified for r in results),
                               results=results, pre_submit=check)

    async def close(self) -> None:
        if self.context and not self._keep_safari_window:
            try:
                await self.context.close()
            except Exception:
                pass
        if self.browser and self.browser.is_connected():
            try:
                await self.browser.close()
            except Exception:
                pass
        if self.playwright:
            try:
                await self.playwright.stop()
            except Exception:
                pass
        self.playwright = None; self.browser = None; self.context = None; self.page = None; self.session_id = None
        self._keep_safari_window = False
        self.target = ApplicationTarget()
        self._stalled_navigation.clear()


browser_demo = BrowserDemoService()
