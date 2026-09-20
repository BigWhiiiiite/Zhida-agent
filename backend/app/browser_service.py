from __future__ import annotations

import hashlib
import ipaddress
import os
import re
import socket
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from .application_models import (ApplicationWorkflowState, RegistrationCredentialsRequest,
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


SENSITIVE_FIELD_HINTS = {
    "authorization", "authorized", "sponsorship", "sponsor", "visa", "salary", "compensation",
    "gender", "sex", "race", "ethnicity", "disability", "veteran", "consent", "agree", "agreement",
    "privacy", "terms", "legal", "work permit", "right to work",
    "性别", "薪资", "期望薪资", "签证", "担保", "工作许可", "残障", "退伍", "族裔", "种族", "同意", "隐私", "条款", "法律声明",
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
OPTION_ALIASES = (
    {"男", "male", "man"}, {"女", "female", "woman"},
    {"中国", "中国大陆", "中华人民共和国", "china", "mainlandchina", "chn"},
    {"远程", "线上", "远程面试", "线上面试", "remote", "online"},
    {"英语", "英文", "english"}, {"普通话", "中文", "汉语", "mandarin", "chinese"},
)
SAFE_RESUME_PARSE_LABEL = re.compile(
    r"^(?:开始)?(?:解析简历|简历解析|智能解析|自动解析|上传并解析|导入简历|从简历导入|"
    r"使用简历填写|一键填充|自动填充)(?:并填充|并导入)?$",
    re.I,
)


def _normalized_option(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def _option_alias(value: str) -> int:
    normalized = _normalized_option(value)
    for index, group in enumerate(OPTION_ALIASES):
        if normalized in {_normalized_option(item) for item in group}:
            return index
    return -1


def _option_matches(wanted: str, actual: str) -> bool:
    left, right = _normalized_option(wanted), _normalized_option(actual)
    if not left or not right or right in OPTION_PLACEHOLDERS:
        return False
    left_alias, right_alias = _option_alias(wanted), _option_alias(actual)
    if left_alias >= 0 or right_alias >= 0:
        return left_alias >= 0 and left_alias == right_alias
    return left == right or left in right or right in left


def _best_option(wanted: str, candidates: list[str]) -> str:
    exact = [item for item in candidates if _normalized_option(item) == _normalized_option(wanted)]
    if exact:
        return exact[0]
    matched = [item for item in candidates if _option_matches(wanted, item)]
    return matched[0] if len(matched) == 1 else ""


def _split_values(value: str | bool) -> list[str]:
    return [part.strip() for part in re.split(r"[,，\n]", str(value)) if part.strip()]


def _selected_values_match(expected: list[str], actual: str) -> bool:
    actual_values = _split_values(actual)
    return all(any(_option_matches(wanted, item) for item in actual_values) or _option_matches(wanted, actual)
               for wanted in expected)


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
            "explicit": .98, "aria-labelledby": .96, "nearby": .92, "aria": .86,
            "attribute": .78, "context": .72, "placeholder": .62, "name": .45,
            "generated": .1, "unknown": .1,
        }.get(source, .5)
        raw_confidence = item.get("recognition_confidence")
        try:
            confidence = float(raw_confidence) if raw_confidence not in (None, "") else default_confidence
        except (TypeError, ValueError):
            confidence = default_confidence
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
            key = f"name:{name}" if name else ""
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
            for value in (item.get("group_label"), item.get("context"), item.get("section"))
        ]
        prompt = ""
        for candidate in candidates:
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
            item["group_label"] = prompt
            item["question_text"] = prompt
            item["label"] = prompt
            if not prompt.startswith("未识别的"):
                item["label_source"] = "nearby"
                item["recognition_confidence"] = max(float(item.get("recognition_confidence") or 0), .92)
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


class BrowserDemoService:
    def __init__(self) -> None:
        self.playwright: Playwright | None = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.session_id: str | None = None

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
        try:
            addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
            for item in addresses:
                ip = ipaddress.ip_address(item[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    raise ValueError("Demo 只允许访问公开招聘网站")
        except socket.gaierror as exc:
            raise ValueError("无法解析这个网站地址") from exc
        return url.strip()

    async def start(self, url: str, user_id: str = "local") -> BrowserSnapshot:
        url = self._validate_url(url)
        await self.close()
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
        return await inspect_application_page(page, session_id)

    async def advance_workflow(self, session_id: str,
                               request: WorkflowAdvanceRequest) -> ApplicationWorkflowState:
        page = self._require(session_id)
        if request.intent == "start_application":
            await start_application(page)
        elif request.intent == "create_account":
            await create_account(page)
        elif request.intent == "continue_application":
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
        return await inspect_application_page(page, session_id)

    async def fill_registration(self, session_id: str,
                                request: RegistrationCredentialsRequest) -> ApplicationWorkflowState:
        page = self._require(session_id)
        password = request.password.get_secret_value() if request.password else ""
        try:
            await fill_registration_info(page, request.email.strip(), request.phone.strip(), password)
        finally:
            password = ""
        return await inspect_application_page(page, session_id)

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
        return await inspect_application_page(page, session_id)

    async def request_code(self, session_id: str, request: VerificationRequest,
                           phone: str, email: str) -> ApplicationWorkflowState:
        page = self._require(session_id)
        value = request.value.strip() or (phone if request.channel == "phone" else email)
        await request_verification_code(page, request.channel, value)
        return await inspect_application_page(page, session_id)

    async def snapshot(self) -> BrowserSnapshot:
        if not self.page or not self.session_id:
            raise LookupError("浏览器会话尚未启动")
        site_route = await route_for_page(self.page)
        policy = policy_for(site_route.adapter)
        recognition_profile = policy.field_profile()
        data = await self.page.evaluate("""
        profile => {
          const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
          const labelText = node => {
            if (!node) return '';
            const clone = node.cloneNode(true);
            clone.querySelectorAll('input, select, textarea, option, button, [role="option"], [role="radio"], [role="checkbox"]').forEach(item => item.remove());
            return clean(clone.innerText || clone.textContent);
          };
          const normalizeFieldText = value => clean(value).replace(/[＊*]+\s*$/g, '').trim();
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
            let score = ({explicit: 42, 'aria-labelledby': 40, nearby: 34, preceding: 31,
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
            return ranked[0] && ranked[0].score > -20 ? ranked[0] : {text: '', source: 'unknown', score: -1000};
          };
          const placeholder = value => /^(select|select one|choose|choose one|please choose|请选择|请选择一项|暂未选择|未选择|点击选择|搜索并选择)[.\\s…]*$/i.test(clean(value));
          const customWrapperSelector = [
            ...(profile?.control_selectors || []),
            '.ant-select-selector', '.arco-select-view', '.el-select__wrapper', '.ivu-select-selection',
            '.semi-select', '.t-select__wrap', '[class*="select-selector"]',
            '[class*="select__selector"]', '[class*="select-view"]',
            '[class*="cascader-picker"]', '[class*="picker-input"]'
          ].join(', ');
          const customSelector = '[role="combobox"], [aria-haspopup="listbox"], ' + customWrapperSelector;
          const controlSelector = 'input, select, textarea, [role="radio"], [role="checkbox"], [role="combobox"], [aria-haspopup="listbox"]';
          const fieldContainer = el => el.closest([
            ...(profile?.question_containers || []),
            '.application-question', '.application-additional', 'fieldset', '[role="radiogroup"]', '[role="group"]',
            '.form-field', '.field', '.ant-form-item', '.arco-form-item', '.el-form-item',
            '.form-group', '.atsx-form-item', '[class*="form-item"]', '[class*="formItem"]',
            '[class*="form_item"]', '[class*="question-item"]', '[class*="questionItem"]',
            '[data-qa*="question"]', '[data-field]'
          ].join(', '));
          const semanticContainer = el => {
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
          const inputFor = el => el.matches('input, select, textarea') ? el : el.querySelector('input, select, textarea');
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
            ? 'input[type="radio"], [role="radio"]'
            : 'input[type="checkbox"], [role="checkbox"]';
          const visibleChoices = (root, fieldType) => [...(root?.querySelectorAll(choiceSelectorFor(fieldType)) || [])]
            .filter(item => !item.disabled && (item.getClientRects().length || item.closest('label')?.getClientRects().length || item.parentElement?.getClientRects().length));
          const commonAncestor = nodes => {
            let candidate = nodes[0] || null;
            while (candidate && !nodes.every(node => candidate.contains(node))) candidate = candidate.parentElement;
            return candidate;
          };
          const choiceMembersFor = (el, fieldType) => {
            const input = inputFor(el);
            const target = input && (input.type || '').toLowerCase() === fieldType ? input : el;
            if (target.name && target.matches(`input[type="${fieldType}"]`)) {
              const scope = target.form || document;
              const named = [...scope.querySelectorAll(`input[type="${fieldType}"][name="${CSS.escape(target.name)}"]`)]
                .filter(item => !item.disabled);
              if (named.length) return named;
            }
            const declared = el.closest(fieldType === 'radio' ? '[role="radiogroup"]' : '[role="group"]');
            const declaredMembers = visibleChoices(declared, fieldType);
            if (declaredMembers.length >= 2) return declaredMembers;
            let node = el.parentElement;
            for (let depth = 0; node && depth < 9 && node !== document.body; depth += 1, node = node.parentElement) {
              const members = visibleChoices(node, fieldType);
              if (members.length >= 2 && members.length <= 20) return members;
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
          const choiceGroupInfoFor = (el, fieldType) => {
            if (!['radio', 'checkbox'].includes(fieldType)) return null;
            const members = choiceMembersFor(el, fieldType);
            let group = commonAncestor(members);
            if (!group || group === document || group === document.documentElement || group === document.body) {
              group = el.closest('[role="radiogroup"], fieldset, [role="group"]') || el.parentElement;
            }
            const options = members.map(item => clean(
              labelText(item.id ? document.querySelector(`label[for="${CSS.escape(item.id)}"]`) : null) ||
              labelText(item.closest('label')) || item.getAttribute('aria-label') || item.innerText || item.value
            )).filter(Boolean);
            const questionSelector = [
              ...(profile?.label_selectors || []),
              'legend', '.application-label', '.question-label', '[data-qa="question-label"]',
              '.ant-form-item-label', '.arco-form-label-item', '.el-form-item__label',
              '[class*="form-label"]', '[class*="field-label"]', '[class*="item-label"]',
              '[class*="formLabel"]', '[class*="fieldLabel"]', '[class*="questionTitle"]',
              '[class*="question-title"]', '[data-testid*="question"]'
            ].join(', ');
            const candidates = [
              {text: labelledBy(group), source: 'aria-labelledby'},
              {text: group?.getAttribute('aria-label'), source: 'aria'},
              ...[...(group?.querySelectorAll(questionSelector) || [])]
                .map(node => ({text: labelText(node), source: 'nearby'})),
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
            const question = bestFieldText(cleanedCandidates, options).text;
            const marker = group ? (group.getAttribute('data-zhida-choice-group') ||
              `zhida-choice-${Date.now()}-${choiceGroupSequence++}`) : '';
            if (group && marker) group.setAttribute('data-zhida-choice-group', marker);
            if (group && question) group.setAttribute('data-zhida-choice-label', question);
            return {group, members, options, question, key: marker};
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
            const question = semanticContainer(el);
            const questionLabel = question?.querySelector('.application-label, legend, .question-label, [data-qa="question-label"]');
            const optionLabel = labelText(explicit) || labelText(wrapping);
            const groupLabel = clean(choiceInfo?.question || questionLabel?.innerText || nearbyLabel(el));
            const internalName = (target.getAttribute('name') || '').includes('[');
            if (choiceInfo?.question) {
              return {text: clean(choiceInfo.question), source: 'nearby', confidence: .96};
            }
            const candidates = [
              {text: labelText(explicit), source: 'explicit'},
              {text: labelText(wrapping), source: 'explicit'},
              {text: labelledBy(target) || labelledBy(el), source: 'aria-labelledby'},
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
            const candidate = bestFieldText(candidates, choiceInfo?.options || []);
            const confidence = Math.max(.1, Math.min(.99, (candidate.score + 20) / 85));
            return {text: candidate.text, source: candidate.source, confidence};
          };
          const groupLabelFor = (el, choiceInfo) => {
            if (choiceInfo?.question) return clean(choiceInfo.question);
            const group = semanticContainer(el);
            return clean(group?.querySelector('.application-label, legend, .question-label, [data-qa="question-label"], .ant-form-item-label, .arco-form-label-item, .el-form-item__label, [class*="form-label"], [class*="field-label"], [class*="formLabel"], [class*="fieldLabel"], [class*="questionTitle"]')?.innerText || nearbyLabel(el));
          };
          const optionLabelFor = el => {
            const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
            return clean(labelText(explicit) || labelText(el.closest('label')) || el.getAttribute('aria-label') || el.innerText || el.value);
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
            'input, select, textarea, [role="radio"], [role="checkbox"], ' + customSelector
          )];
          const canonical = el => {
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
          const elements = [...new Set(candidates.map(canonical))]
            .filter(el => {
              const role = (el.getAttribute('role') || '').toLowerCase();
              const type = (el.type || '').toLowerCase();
              const customSelect = role === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox' || el.matches(customSelector);
              const optionInput = ['radio', 'checkbox'].includes(type) || ['radio', 'checkbox'].includes(role);
              if (!customSelect && !optionInput && ['hidden','submit','button','image','reset','password'].includes(type)) return false;
              const optionVisible = optionInput && (el.closest('label')?.getClientRects().length || el.parentElement?.getClientRects().length);
              return !el.disabled && (type === 'file' || el.getClientRects().length > 0 || optionVisible);
            });
          let containerSequence = 0;
          const fields = elements.map((el, index) => {
            const marker = el.getAttribute('data-zhida-field') ||
              `zhida-${Date.now()}-${index}-${Math.random().toString(36).slice(2)}`;
            el.setAttribute('data-zhida-field', marker);
            const input = inputFor(el);
            const role = (el.getAttribute('role') || '').toLowerCase();
            const customSelect = role === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox' || el.matches(customSelector);
            const fieldType = customSelect ? 'combobox' : (['radio', 'checkbox'].includes(role) ? role : (el.type || el.tagName).toLowerCase());
            const choiceInfo = choiceGroupInfoFor(el, fieldType);
            const labelInfo = labelInfoFor(el, fieldType, choiceInfo);
            const label = labelInfo.text.slice(0, 500);
            const nearbyLabels = nearbyLabelCandidates(el);
            const sectionPath = sectionPathFor(el);
            const entityContainer = entityContainerFor(el);
            const containerMarker = entityContainer ? (entityContainer.getAttribute('data-zhida-container') ||
              `zhida-container-${Date.now()}-${containerSequence++}`) : '';
            if (entityContainer && containerMarker) entityContainer.setAttribute('data-zhida-container', containerMarker);
            const context = clean(choiceInfo?.question || contextFor(el)).slice(0, 600);
            const placeholderText = clean(input?.getAttribute('placeholder') || el.getAttribute('placeholder')).slice(0, 300);
            let options = el.tagName === 'SELECT' ? [...el.options].filter(o => !o.disabled && !o.parentElement?.disabled).map(o => clean(o.text)).filter(value => value && !placeholder(value)) : [];
            if (['radio', 'checkbox'].includes(fieldType)) {
              options = choiceInfo?.options || [];
            }
            // Custom options are read through the same scoped tool used by execution.
            const selectedItems = customSelect ? [...el.querySelectorAll('[class*="selection-item"], [class*="selected-value"], [class*="selected-item"]')]
              .map(item => clean(item.innerText || item.textContent)).filter(value => value && !placeholder(value)) : [];
            const customValue = selectedItems.join(', ') || clean(el.getAttribute('aria-valuetext') || el.querySelector('input')?.value || el.innerText);
            return {
              selector: `[data-zhida-field="${marker}"]`, label, question_text: label,
              label_source: labelInfo.source, recognition_confidence: labelInfo.confidence || 0,
              context, help_text: helpTextFor(el), nearby_labels: nearbyLabels,
              section_path: sectionPath, placeholder: placeholderText, ordinal: index + 1,
              name: el.getAttribute('name') || el.querySelector('input')?.getAttribute('name') || el.getAttribute('id') || '', field_type: fieldType,
              required: el.required || el.querySelector('input')?.required || el.getAttribute('aria-required') === 'true' ||
                choiceInfo?.group?.getAttribute('aria-required') === 'true' || choiceInfo?.members.some(item => item.required) || /[*✱]/.test(label), options,
              current_value: el.type === 'file' ? [...(el.files || [])].map(file => file.name).join(', ') :
                (el.tagName === 'SELECT' ? [...el.selectedOptions]
                  .map(option => clean(option.textContent || option.value)).filter(value => value && !placeholder(value)).join(', ') :
                (['checkbox','radio'].includes(fieldType) ? String(el.checked ?? el.getAttribute('aria-checked') === 'true') :
                  clean(el.value || (customSelect ? customValue : '')))),
              accept: el.getAttribute('accept') || '', role,
              group_label: groupLabelFor(el, choiceInfo).slice(0, 500),
              option_label: ['radio','checkbox'].includes(fieldType) ? optionLabelFor(el).slice(0, 500) : '',
              option_value: ['radio','checkbox'].includes(fieldType) ? clean(el.value || el.getAttribute('data-value') || el.innerText) : '',
              multiple: Boolean(el.multiple || el.getAttribute('aria-multiselectable') === 'true' ||
                /multiple|multi|tags/.test(String(el.className || '').toLowerCase()) ||
                /multiple|multi|tags/.test(String(el.closest('[class]')?.className || '').toLowerCase())),
              readonly: Boolean(el.readOnly || el.querySelector('input')?.readOnly || el.getAttribute('aria-readonly') === 'true'),
              autocomplete: input?.getAttribute('autocomplete') || el.getAttribute('autocomplete') || '',
              section: (sectionFor(el) || sectionPath[sectionPath.length - 1] || '').slice(0, 500), container_key: containerMarker,
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
          return [...fields, ...expanders];
        }
        """, recognition_profile)
        # Component libraries often render options only after the combobox opens.
        # Opening a list is read-only and lets the review UI present the real choices.
        for item in data:
            if item.get("field_type") != "combobox" or item.get("options"):
                continue
            try:
                locator = self.page.locator(item["selector"]).first
                before_open = await visible_popup_ids(self.page, policy)
                already_open = await scoped_option_entries(self.page, locator, policy)
                if already_open:
                    entries = already_open
                else:
                    await locator.click(timeout=1800)
                    entries = await self._wait_for_options(locator, policy, before_open)
                item["options"] = list(dict.fromkeys(text for text, _ in entries))
                await self.page.keyboard.press("Escape")
            except Exception:
                item["options"] = item.get("options", [])
        _finalize_field_metadata(data)
        _finalize_choice_metadata(data)
        fields = enrich_fields([PageField.model_validate(item) for item in data], self.page.url)
        return BrowserSnapshot(
            session_id=self.session_id, url=self.page.url, title=await self.page.title(),
            recognition_profile=str(recognition_profile["name"]), site_route=site_route, fields=fields,
        )

    async def snapshot_for(self, session_id: str) -> BrowserSnapshot:
        self._require(session_id)
        return await self.snapshot()

    async def inspect_field(self, session_id: str, selector: str) -> BrowserSnapshot:
        """Bring one collected field into view and retry read-only option discovery."""
        page = self._require(session_id)
        before = await self.snapshot()
        field = next((item for item in before.fields if item.selector == selector), None)
        if not field:
            raise LookupError("字段已经变化，请重新分析当前页面")
        if field.field_type in {"file", "section-button"}:
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
            try:
                await locator.click(timeout=3000)
                await page.wait_for_timeout(450)
            except Exception:
                pass
        refreshed = await self.snapshot()
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
        return refreshed

    async def expand_section(self, session_id: str, selector: str) -> BrowserSnapshot:
        page = self._require(session_id)
        snapshot = await self.snapshot()
        field = next((item for item in snapshot.fields if item.selector == selector), None)
        if not field or field.field_type != "section-button":
            raise ValueError("这不是可以安全展开的资料栏目")
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
                                             resume_path: Path) -> NativeResumeImportResult:
        """Upload a resume and let the ATS parser produce a first draft.

        Only narrowly named resume-parse/import controls may be clicked. Generic
        confirm/next/save/apply buttons remain a manual gate.
        """
        page = self._require(session_id)
        before = await self.snapshot()
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
        if resume_path.name not in uploaded:
            raise RuntimeError("简历已选择，但招聘网页没有保留该文件")

        await page.wait_for_timeout(1800)
        after = await self.snapshot()
        changed = _changed_field_count(before, after)
        trigger_clicked = False
        trigger_label = ""
        if changed == 0:
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
            '.ant-select-selector', '.arco-select-view', '.el-select__wrapper', '.ivu-select-selection',
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
          const canonical = el => el.closest(customWrapperSelector) || (el.matches(customSelector) ? el : (el.closest(customSelector) || el));
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
          const validationErrors = [...document.querySelectorAll('[aria-invalid="true"], .error-message, .field-error, .application-error')]
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
          return {required_total: requiredTotal, filled_count: filledCount, required_missing: missing,
            validation_errors: [...new Set(validationErrors)], human_challenges: humanChallenges,
            file_uploads: fileUploads, submit_labels: submitLabels};
        }
        """, policy.field_profile())
        missing_rows = data["required_missing"]
        _finalize_field_metadata(missing_rows)
        missing = [RequiredFieldIssue.model_validate(item) for item in missing_rows]
        return PreSubmitCheck(url=page.url, ready=not missing and not data["validation_errors"] and not data["human_challenges"],
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
        for elapsed in range(0, policy.option_wait_ms + 1, 100):
            entries = await scoped_option_entries(self.page, control, policy, before_open)
            if entries and (not wanted or _best_option(wanted, [text for text, _ in entries])):
                return entries
            if elapsed < policy.option_wait_ms:
                await self.page.wait_for_timeout(100)
        return entries

    async def _visible_option_entries(self, control):
        if not self.page:
            return []
        policy = policy_for((await route_for_page(self.page)).adapter)
        return await scoped_option_entries(self.page, control, policy)

    async def _select_custom(self, field: PageField, values: list[str]) -> list[str]:
        selected: list[str] = []
        policy = policy_for((await route_for_page(self.page)).adapter)
        targets = values if field.multiple else values[:1]
        for wanted in targets:
            live_field, control = await self._resolve_field(field)
            await control.scroll_into_view_if_needed(timeout=3000)
            before_open = await visible_popup_ids(self.page, policy)
            entries = await scoped_option_entries(self.page, control, policy)
            if not entries:
                await control.click(timeout=8000)
                entries = await self._wait_for_options(control, policy, before_open, wanted)
            match = _best_option(wanted, [text for text, _ in entries])
            if not match:
                search = control if await control.evaluate("el => el.tagName === 'INPUT'") else control.locator("input").first
                if await search.count() and await search.is_editable():
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
            await option.click(timeout=8000)
            selected.append(match)
            await self.page.wait_for_timeout(250)
            field = live_field
        return selected

    async def _select_native(self, field: PageField, values: list[str]) -> list[str]:
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
                selected.append(next(item["label"] for item in options if item["value"] == value_match))
                continue
            raise ValueError(f"网页下拉选项中找不到“{wanted}”")
        await control.select_option(label=selected if field.multiple else selected[0], timeout=8000)
        return selected

    async def _read_field_value(self, field: PageField) -> str:
        live_field, control = await self._resolve_field(field)
        if live_field.field_type in {"checkbox", "radio"}:
            checked = await control.evaluate(
                "el => 'checked' in el ? Boolean(el.checked) : el.getAttribute('aria-checked') === 'true'"
            )
            return str(bool(checked)).lower()
        if live_field.field_type in {"select-one", "select-multiple"}:
            selected = [item.strip() for item in await control.locator("option:checked").all_text_contents()]
            return ", ".join(item for item in selected if item) or await control.input_value()
        if live_field.field_type == "combobox":
            return await control.evaluate("""el => {
              const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
              const selected = [...el.querySelectorAll('[class*="selection-item"], [class*="selected-value"], [class*="selected-item"]')]
                .map(item => clean(item.innerText || item.textContent)).filter(Boolean);
              return selected.join(', ') || clean(el.getAttribute('aria-valuetext') || el.querySelector('input')?.value || el.innerText);
            }""")
        return await control.input_value()

    async def execute(self, session_id: str, request: ExecutePlanRequest,
                      resume_path: Path | None = None) -> ExecutionResult:
        page = self._require(session_id)
        snapshot = await self.snapshot()
        fields = {field.selector: field for field in snapshot.fields}
        results: list[ActionResult] = []
        if resume_path:
            resume_fields = [field for field in snapshot.fields if field.field_type == "file" and
                             any(hint in f"{field.label} {field.name}".lower()
                                 for hint in ("resume", "cv", "curriculum", "简历"))]
            for field in resume_fields[:1]:
                try:
                    await page.locator(field.selector).first.set_input_files(str(resume_path), timeout=10000)
                    actual = await page.locator(field.selector).first.evaluate(
                        "el => [...(el.files || [])].map(file => file.name).join(', ')")
                    verified = resume_path.name in actual
                    results.append(ActionResult(selector=field.selector, label=field.label or "Resume/CV",
                                                status="filled" if verified else "failed", verified=verified,
                                                actual_value=actual, message="简历已上传" if verified else "简历上传后未能验证"))
                except Exception as exc:
                    results.append(ActionResult(selector=field.selector, label=field.label or "Resume/CV",
                                                status="failed", message=str(exc)[:240]))
        for action in request.actions:
            field = fields.get(action.selector)
            field_description = " ".join(filter(None, (
                field.section, field.group_label, field.label, field.name
            ))).lower() if field else ""
            deterministically_sensitive = any(hint in field_description for hint in SENSITIVE_FIELD_HINTS)
            deterministically_manual = any(hint in field_description for hint in MANUAL_CONFIRM_FIELD_HINTS)
            compatible = bool(field) and (
                (action.action == "fill" and field.field_type not in {"checkbox", "radio", "select-one", "select-multiple", "combobox"})
                or (action.action == "select" and field.field_type in {"select-one", "select-multiple", "combobox"})
                or (action.action == "check" and field.field_type in {"checkbox", "radio"})
            )
            unsafe = (
                field is None
                or action.action not in {"fill", "select", "check"}
                or not compatible
                or (action.action in {"fill", "select"} and not str(action.value).strip())
                or (action.sensitive and not action.user_confirmed)
                or (deterministically_sensitive and not action.user_confirmed)
                or (deterministically_manual and not action.user_confirmed)
                or action.confidence < request.min_confidence
            )
            if unsafe:
                results.append(ActionResult(selector=action.selector, label=action.label, status="skipped",
                                            message="需要确认、敏感或置信度不足"))
                continue
            try:
                field, locator = await self._resolve_field(field)
                expected_values = _split_values(action.value)
                if action.action == "fill":
                    await locator.fill(str(action.value), timeout=8000)
                elif action.action == "select":
                    if field.field_type == "combobox":
                        await self._select_custom(field, expected_values[:20])
                    else:
                        await self._select_native(field, expected_values)
                elif action.action == "check":
                    native_check = await locator.evaluate("el => el.tagName === 'INPUT'")
                    if native_check:
                        try:
                            await locator.set_checked(bool(action.value), timeout=8000)
                        except Exception:
                            await locator.evaluate("""(el, checked) => {
                              el.checked = checked;
                              el.dispatchEvent(new Event('input', {bubbles: true}));
                              el.dispatchEvent(new Event('change', {bubbles: true}));
                            }""", bool(action.value))
                    else:
                        checked = await locator.get_attribute("aria-checked") == "true"
                        if checked != bool(action.value):
                            await locator.click(timeout=8000)
                actual = await self._read_field_value(field)
                if field.field_type in {"checkbox", "radio"}:
                    expected = str(bool(action.value)).lower()
                    verified = actual.strip() == expected.strip()
                elif field.field_type in {"select-one", "select-multiple", "combobox"}:
                    expected = str(action.value)
                    verified = _selected_values_match(expected_values, actual)
                else:
                    expected = str(action.value)
                    verified = actual.strip() == expected.strip()
                results.append(ActionResult(selector=action.selector, label=action.label,
                                            status="filled" if verified else "failed", verified=verified,
                                            actual_value=actual,
                                            message="回读验证成功" if verified else f"回读值不一致，期望 {expected}"))
            except Exception as exc:
                results.append(ActionResult(selector=action.selector, label=action.label, status="failed",
                                            message=str(exc)[:240]))
        await page.wait_for_timeout(800)
        # A reactive ATS may normalize or clear a value after the input event.
        # Re-read every planned action after the page settles so the result is
        # based on final DOM state instead of an optimistic immediate read.
        actions_by_selector = {action.selector: action for action in request.actions}
        for result in results:
            action = actions_by_selector.get(result.selector)
            field = fields.get(result.selector)
            if result.status != "filled" or not action or not field:
                continue
            try:
                expected = str(action.value)
                if field.field_type in {"checkbox", "radio"}:
                    result.actual_value = await self._read_field_value(field)
                    result.verified = result.actual_value == str(bool(action.value)).lower()
                elif field.field_type in {"select-one", "select-multiple", "combobox"}:
                    result.actual_value = await self._read_field_value(field)
                    result.verified = _selected_values_match(_split_values(action.value), result.actual_value)
                else:
                    result.actual_value = await self._read_field_value(field)
                    result.verified = result.actual_value.strip() == expected.strip()
                if not result.verified:
                    result.status = "failed"
                    result.message = f"页面稳定后回读值不一致，期望 {expected}"
            except Exception as exc:
                result.status = "failed"
                result.verified = False
                result.message = str(exc)[:240]
        check = await self.pre_submit_check(session_id)
        return ExecutionResult(url=page.url, completed=sum(r.status == "filled" for r in results),
                               skipped=sum(r.status == "skipped" for r in results),
                               failed=sum(r.status == "failed" for r in results),
                               verified=sum(r.verified for r in results),
                               unverified=sum(r.status == "filled" and not r.verified for r in results),
                               results=results, pre_submit=check)

    async def close(self) -> None:
        if self.context:
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


browser_demo = BrowserDemoService()
