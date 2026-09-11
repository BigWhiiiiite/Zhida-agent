from __future__ import annotations

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
from .ats_adapters import (continue_application, create_account, fill_registration_info,
                           fill_verification_code, inspect_application_page,
                           request_verification_code, start_application)
from .browser_models import (ActionResult, BrowserSnapshot, ExecutePlanRequest, ExecutionResult,
                             NativeResumeImportResult, PageField, PreSubmitCheck, RequiredFieldIssue)


SENSITIVE_FIELD_HINTS = {
    "authorization", "authorized", "sponsorship", "sponsor", "visa", "salary", "compensation",
    "gender", "sex", "race", "ethnicity", "disability", "veteran", "consent", "agree", "agreement",
    "privacy", "terms", "legal", "work permit", "right to work",
    "性别", "薪资", "期望薪资", "签证", "担保", "工作许可", "残障", "退伍", "族裔", "种族", "同意", "隐私", "条款", "法律声明",
    "emergency contact", "next of kin", "guardian", "referee", "recommender",
    "紧急联系人", "紧急联络人", "家属联系人", "监护人", "推荐人", "证明人",
}

MANUAL_CONFIRM_FIELD_HINTS = {
    "是否", "愿意", "调剂", "服从分配", "意向事业群", "感兴趣的事业群", "志愿", "偏好",
    "would you", "are you willing", "preference", "preferred business", "business group", "relocate",
}

OPTION_LOCATOR_SELECTOR = ", ".join((
    '[role="option"]', '[role="listbox"] option', '.ant-select-item-option',
    '.arco-select-option', '.el-select-dropdown__item', '.ivu-select-item',
    '.semi-select-option', '[class*="select-option"]', '[class*="dropdown-item"]',
))
OPTION_PLACEHOLDERS = {
    "", "select", "selectone", "choose", "chooseone", "pleasechoose", "请选择", "请选择一项",
    "暂未选择", "未选择", "点击选择", "搜索并选择",
}
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
        field.section, field.group_label, field.label, field.name, field.field_type,
    ))))


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

    async def start(self, url: str) -> BrowserSnapshot:
        url = self._validate_url(url)
        await self.close()
        self.playwright = await async_playwright().start()
        try:
            channel = os.getenv("APP_BROWSER_CHANNEL", "chrome")
            self.browser = await self.playwright.chromium.launch(channel=channel, headless=False)
        except Exception as exc:
            await self.close()
            raise RuntimeError("无法启动浏览器，请确认已安装 Chrome，或运行 playwright install chromium 并设置 APP_BROWSER_CHANNEL=chromium") from exc
        self.context = await self.browser.new_context(viewport={"width": 1280, "height": 850})
        self.page = await self.context.new_page()
        self.session_id = str(uuid4())
        try:
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
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
        data = await self.page.evaluate("""
        () => {
          const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
          const labelText = node => {
            if (!node) return '';
            const clone = node.cloneNode(true);
            clone.querySelectorAll('input, select, textarea, option, button, [role="option"]').forEach(item => item.remove());
            return clean(clone.innerText || clone.textContent);
          };
          const placeholder = value => /^(select|select one|choose|choose one|please choose|请选择|请选择一项|暂未选择|未选择|点击选择|搜索并选择)[.\\s…]*$/i.test(clean(value));
          const customWrapperSelector = [
            '.ant-select-selector', '.arco-select-view', '.el-select__wrapper', '.ivu-select-selection',
            '.semi-select', '.t-select__wrap', '[class*="select-selector"]',
            '[class*="select__selector"]', '[class*="select-view"]',
            '[class*="cascader-picker"]', '[class*="picker-input"]'
          ].join(', ');
          const customSelector = '[role="combobox"], [aria-haspopup="listbox"], ' + customWrapperSelector;
          const fieldContainer = el => el.closest([
            '.application-question', '.application-additional', 'fieldset', '[role="radiogroup"]', '[role="group"]',
            '.form-field', '.field', '.ant-form-item', '.arco-form-item', '.el-form-item',
            '[class*="form-item"]', '[class*="formItem"]', '[data-qa*="question"]'
          ].join(', '));
          const labelledBy = el => clean((el.getAttribute('aria-labelledby') || '').split(/\\s+/)
            .map(id => document.getElementById(id)?.innerText || '').join(' '));
          const nearbyLabel = el => {
            const container = fieldContainer(el);
            if (!container) return '';
            const selector = [
              'legend', '.application-label', '.question-label', '[data-qa="question-label"]',
              '.ant-form-item-label', '.arco-form-label-item', '.el-form-item__label',
              '[class*="form-label"]', '[class*="field-label"]', '[class*="item-label"]'
            ].join(', ');
            return [...container.querySelectorAll(selector)]
              .map(item => clean(item.innerText || item.textContent))
              .find(value => value && value.length <= 300) || '';
          };
          const labelFor = el => {
            const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
            const wrapping = el.closest('label');
            const question = fieldContainer(el);
            const questionLabel = question?.querySelector('.application-label, legend, .question-label, [data-qa="question-label"]');
            const optionLabel = labelText(explicit) || labelText(wrapping);
            const groupLabel = clean(questionLabel?.innerText || nearbyLabel(el));
            const internalName = (el.getAttribute('name') || '').includes('[');
            if ((internalName || ['radio', 'checkbox', 'file'].includes(el.type)) && groupLabel) {
              return clean(groupLabel === optionLabel ? groupLabel : `${groupLabel}${optionLabel ? ` — ${optionLabel}` : ''}`);
            }
            return clean(labelText(explicit) || labelText(wrapping) || labelledBy(el) ||
              el.getAttribute('aria-label') || groupLabel || nearbyLabel(el) ||
              el.getAttribute('placeholder') || el.getAttribute('name'));
          };
          const groupLabelFor = el => {
            const group = fieldContainer(el);
            return clean(group?.querySelector('.application-label, legend, .question-label, [data-qa="question-label"], .ant-form-item-label, .arco-form-label-item, .el-form-item__label, [class*="form-label"], [class*="field-label"]')?.innerText || nearbyLabel(el));
          };
          const optionLabelFor = el => {
            const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
            return clean(labelText(explicit) || labelText(el.closest('label')) || el.getAttribute('aria-label') || el.innerText || el.value);
          };
          const sectionFor = el => {
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
          const fields = elements.map((el, index) => {
            const marker = el.getAttribute('data-zhida-field') ||
              `zhida-${Date.now()}-${index}-${Math.random().toString(36).slice(2)}`;
            el.setAttribute('data-zhida-field', marker);
            const label = labelFor(el).slice(0, 500);
            const role = (el.getAttribute('role') || '').toLowerCase();
            const customSelect = role === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox' || el.matches(customSelector);
            const fieldType = customSelect ? 'combobox' : (['radio', 'checkbox'].includes(role) ? role : (el.type || el.tagName).toLowerCase());
            let options = el.tagName === 'SELECT' ? [...el.options].map(o => clean(o.text)).filter(value => value && !placeholder(value)) : [];
            if (['radio', 'checkbox'].includes(fieldType)) {
              const group = fieldContainer(el) || el.closest('[role="radiogroup"], [role="group"]');
              const members = el.name ? [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)] :
                [...(group?.querySelectorAll(`[role="${fieldType}"]`) || [el])];
              options = members.map(item => clean(item.closest('label')?.innerText || item.getAttribute('aria-label') || item.innerText || item.value)).filter(Boolean);
            }
            if (customSelect) {
              const controlled = (el.getAttribute('aria-controls') || el.getAttribute('aria-owns') || '')
                .split(/\\s+/).map(id => document.getElementById(id)).filter(Boolean);
              const localOptions = controlled.flatMap(root => [...root.querySelectorAll('[role="option"], option')]);
              options = localOptions.map(item => clean(item.innerText || item.textContent || item.value)).filter(value => value && !placeholder(value));
            }
            const selectedItems = customSelect ? [...el.querySelectorAll('[class*="selection-item"], [class*="selected-value"], [class*="selected-item"]')]
              .map(item => clean(item.innerText || item.textContent)).filter(value => value && !placeholder(value)) : [];
            const customValue = selectedItems.join(', ') || clean(el.getAttribute('aria-valuetext') || el.querySelector('input')?.value || el.innerText);
            return {
              selector: `[data-zhida-field="${marker}"]`, label,
              name: el.getAttribute('name') || el.querySelector('input')?.getAttribute('name') || el.getAttribute('id') || '', field_type: fieldType,
              required: el.required || el.querySelector('input')?.required || el.getAttribute('aria-required') === 'true' || /[*✱]/.test(label), options,
              current_value: el.type === 'file' ? [...(el.files || [])].map(file => file.name).join(', ') :
                (el.tagName === 'SELECT' ? [...el.selectedOptions]
                  .map(option => clean(option.textContent || option.value)).filter(value => value && !placeholder(value)).join(', ') :
                (['checkbox','radio'].includes(fieldType) ? String(el.checked ?? el.getAttribute('aria-checked') === 'true') :
                  clean(el.value || (customSelect ? customValue : '')))),
              accept: el.getAttribute('accept') || '', role,
              group_label: groupLabelFor(el).slice(0, 500),
              option_label: ['radio','checkbox'].includes(fieldType) ? optionLabelFor(el).slice(0, 500) : '',
              option_value: ['radio','checkbox'].includes(fieldType) ? clean(el.value || el.getAttribute('data-value') || el.innerText) : '',
              multiple: Boolean(el.multiple || el.getAttribute('aria-multiselectable') === 'true' ||
                /multiple|multi|tags/.test(String(el.className || '').toLowerCase()) ||
                /multiple|multi|tags/.test(String(el.closest('[class]')?.className || '').toLowerCase())),
              readonly: Boolean(el.readOnly || el.querySelector('input')?.readOnly || el.getAttribute('aria-readonly') === 'true'),
              section: sectionFor(el).slice(0, 500)
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
              selector: `[data-zhida-field="${marker}"]`, label: semanticLabel,
              name: button.getAttribute('name') || button.getAttribute('id') || '',
              field_type: 'section-button', required: false, options: [], current_value: '',
              accept: '', role: 'button', group_label: heading, option_label: '',
              option_value: '', multiple: false, readonly: false, section: heading
            });
          }
          return [...fields, ...expanders];
        }
        """)
        # Component libraries often render options only after the combobox opens.
        # Opening a list is read-only and lets the review UI present the real choices.
        for item in data:
            if item.get("field_type") != "combobox" or item.get("options"):
                continue
            try:
                locator = self.page.locator(item["selector"]).first
                await locator.click(timeout=1800)
                await self.page.wait_for_timeout(180)
                item["options"] = await self.page.evaluate("""
                marker => {
                  const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
                  const placeholder = value => /^(select|select one|choose|choose one|please choose|请选择|请选择一项|暂未选择|未选择|点击选择|搜索并选择)[.\\s…]*$/i.test(clean(value));
                  const el = document.querySelector(`[data-zhida-field="${CSS.escape(marker)}"]`);
                  const ids = (el?.getAttribute('aria-controls') || el?.getAttribute('aria-owns') || '')
                    .split(/\\s+/).filter(Boolean);
                  const controlled = ids.map(id => document.getElementById(id)).filter(Boolean)
                    .flatMap(root => [...root.querySelectorAll('[role="option"], option, .ant-select-item-option, .arco-select-option, .el-select-dropdown__item, .ivu-select-item, .semi-select-option, [class*="select-option"], [class*="dropdown-item"]')]);
                  const visible = [...document.querySelectorAll([
                    '[role="option"]', '[role="listbox"] option', '.ant-select-item-option',
                    '.arco-select-option', '.el-select-dropdown__item', '.ivu-select-item',
                    '.semi-select-option', '[class*="select-option"]', '[class*="dropdown-item"]'
                  ].join(', '))]
                    .filter(option => option.getClientRects().length > 0);
                  return [...new Set(controlled.length ? controlled : visible)]
                    .map(option => clean(option.innerText || option.textContent || option.value))
                    .filter(value => value && value.length <= 300 && !placeholder(value));
                }
                """, item["selector"].split('"')[1])
                await self.page.keyboard.press("Escape")
            except Exception:
                item["options"] = item.get("options", [])
        fields = [PageField.model_validate(item) for item in data]
        return BrowserSnapshot(session_id=self.session_id, url=self.page.url, title=await self.page.title(), fields=fields)

    async def snapshot_for(self, session_id: str) -> BrowserSnapshot:
        self._require(session_id)
        return await self.snapshot()

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
        data = await page.evaluate("""
        () => {
          const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
          const customWrapperSelector = [
            '.ant-select-selector', '.arco-select-view', '.el-select__wrapper', '.ivu-select-selection',
            '.semi-select', '.t-select__wrap', '[class*="select-selector"]',
            '[class*="select__selector"]', '[class*="select-view"]',
            '[class*="cascader-picker"]', '[class*="picker-input"]'
          ].join(', ');
          const customSelector = '[role="combobox"], [aria-haspopup="listbox"], ' + customWrapperSelector;
          const labelFor = el => {
            const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
            const question = el.closest('.application-question, .application-additional, fieldset, [role="radiogroup"], [role="group"], .form-field, .field, .ant-form-item, .arco-form-item, .el-form-item, [class*="form-item"], [class*="formItem"]');
            return clean(explicit?.innerText || question?.querySelector('.application-label, legend, .question-label')?.innerText ||
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
              const group = el.closest('[role="radiogroup"], fieldset, [role="group"]');
              const groupKey = el.name || group?.getAttribute('data-zhida-radio-group') ||
                ('radio-' + [...document.querySelectorAll('[role="radiogroup"], fieldset, [role="group"]')].indexOf(group));
              if (handledRadioNames.has(groupKey)) continue;
              handledRadioNames.add(groupKey);
              const members = el.name ? [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)] :
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
        """)
        missing = [RequiredFieldIssue.model_validate(item) for item in data["required_missing"]]
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

    async def _visible_option_entries(self, control):
        if not self.page:
            return []
        controlled_ids = await control.evaluate("""el => {
          const direct = `${el.getAttribute('aria-controls') || ''} ${el.getAttribute('aria-owns') || ''}`;
          const nested = el.querySelector('[aria-controls], [aria-owns]');
          return `${direct} ${nested?.getAttribute('aria-controls') || ''} ${nested?.getAttribute('aria-owns') || ''}`
            .trim().split(/\s+/).filter(Boolean);
        }""")
        option_locators = []
        for controlled_id in controlled_ids:
            root = self.page.locator(f"#{controlled_id}")
            if await root.count():
                option_locators.append(root.locator(OPTION_LOCATOR_SELECTOR))
        option_locators.append(self.page.locator(OPTION_LOCATOR_SELECTOR))
        entries = []
        seen: set[str] = set()
        for options in option_locators:
            for index in range(min(await options.count(), 300)):
                option = options.nth(index)
                try:
                    if not await option.is_visible():
                        continue
                    text = re.sub(r"\s+", " ", (await option.inner_text()).strip())
                    normalized = _normalized_option(text)
                    if not text or len(text) > 300 or normalized in OPTION_PLACEHOLDERS or normalized in seen:
                        continue
                    seen.add(normalized)
                    entries.append((text, option))
                except Exception:
                    continue
            if entries:
                break
        return entries

    async def _select_custom(self, field: PageField, values: list[str]) -> list[str]:
        selected: list[str] = []
        targets = values if field.multiple else values[:1]
        for wanted in targets:
            live_field, control = await self._resolve_field(field)
            await control.scroll_into_view_if_needed(timeout=3000)
            await control.click(timeout=8000)
            await self.page.wait_for_timeout(300)
            entries = await self._visible_option_entries(control)
            match = _best_option(wanted, [text for text, _ in entries])
            if not match:
                search = control if await control.evaluate("el => el.tagName === 'INPUT'") else control.locator("input").first
                if await search.count() and await search.is_editable():
                    await search.fill(wanted)
                    await self.page.wait_for_timeout(350)
                    entries = await self._visible_option_entries(control)
                    match = _best_option(wanted, [text for text, _ in entries])
            if not match:
                await self.page.keyboard.press("Escape")
                choices = "、".join(text for text, _ in entries[:12]) or "未读取到选项"
                raise ValueError(f"网页选项中找不到“{wanted}”；当前选项：{choices}")
            option = next(option for text, option in entries if text == match)
            await option.click(timeout=8000)
            selected.append(match)
            await self.page.wait_for_timeout(250)
            field = live_field
        return selected

    async def _select_native(self, field: PageField, values: list[str]) -> list[str]:
        _, control = await self._resolve_field(field)
        options = await control.locator("option").evaluate_all(
            "items => items.map(item => ({label: String(item.textContent || '').trim(), value: item.value}))"
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
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self.playwright = None; self.browser = None; self.context = None; self.page = None; self.session_id = None


browser_demo = BrowserDemoService()
