from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from .application_models import (ApplicationWorkflowState, VerificationCodeRequest,
                                 VerificationRequest, WorkflowAdvanceRequest)
from .ats_adapters import (fill_verification_code, inspect_application_page,
                           request_verification_code, start_application)
from .browser_models import (ActionResult, BrowserSnapshot, ExecutePlanRequest, ExecutionResult, PageField,
                             PreSubmitCheck, RequiredFieldIssue)


SENSITIVE_FIELD_HINTS = {
    "authorization", "authorized", "sponsorship", "sponsor", "visa", "salary", "compensation",
    "gender", "sex", "race", "ethnicity", "disability", "veteran", "consent", "agree", "agreement",
    "privacy", "terms", "legal", "work permit", "right to work",
    "性别", "薪资", "期望薪资", "签证", "担保", "工作许可", "残障", "退伍", "族裔", "种族", "同意", "隐私", "条款", "法律声明",
}


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
        return await inspect_application_page(page, session_id)

    async def enter_verification(self, session_id: str,
                                 request: VerificationCodeRequest) -> ApplicationWorkflowState:
        page = self._require(session_id)
        code = request.code.get_secret_value()
        try:
            await fill_verification_code(page, code, request.submit)
        finally:
            code = ""
        return await inspect_application_page(page, session_id)

    async def request_code(self, session_id: str, request: VerificationRequest,
                           phone: str, email: str) -> ApplicationWorkflowState:
        page = self._require(session_id)
        value = phone if request.channel == "phone" else email
        await request_verification_code(page, request.channel, value)
        return await inspect_application_page(page, session_id)

    async def snapshot(self) -> BrowserSnapshot:
        if not self.page or not self.session_id:
            raise LookupError("浏览器会话尚未启动")
        data = await self.page.evaluate("""
        () => {
          const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
          const labelledBy = el => clean((el.getAttribute('aria-labelledby') || '').split(/\\s+/)
            .map(id => document.getElementById(id)?.innerText || '').join(' '));
          const labelFor = el => {
            const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
            const wrapping = el.closest('label');
            const question = el.closest('.application-question, .application-additional, fieldset, [role="group"], .form-field, .field');
            const questionLabel = question?.querySelector('.application-label, legend, .question-label, [data-qa="question-label"]');
            const optionLabel = clean(explicit?.innerText || wrapping?.innerText);
            const groupLabel = clean(questionLabel?.innerText);
            const internalName = (el.getAttribute('name') || '').includes('[');
            if ((internalName || ['radio', 'checkbox', 'file'].includes(el.type)) && groupLabel) {
              return clean(groupLabel === optionLabel ? groupLabel : `${groupLabel}${optionLabel ? ` — ${optionLabel}` : ''}`);
            }
            return clean(explicit?.innerText || wrapping?.innerText || labelledBy(el) ||
              el.getAttribute('aria-label') || groupLabel || el.getAttribute('placeholder') || el.getAttribute('name'));
          };
          const elements = [...document.querySelectorAll('input, select, textarea')]
            .filter(el => !['hidden','submit','button','image','reset','password'].includes((el.type || '').toLowerCase()))
            .filter(el => !el.disabled && (el.type === 'file' || el.getClientRects().length > 0));
          return elements.map((el, index) => {
            const marker = el.getAttribute('data-zhida-field') ||
              `zhida-${Date.now()}-${index}-${Math.random().toString(36).slice(2)}`;
            el.setAttribute('data-zhida-field', marker);
            const label = labelFor(el).slice(0, 500);
            let options = el.tagName === 'SELECT' ? [...el.options].map(o => clean(o.text)).filter(Boolean) : [];
            if (['radio', 'checkbox'].includes(el.type) && el.name) {
              options = [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)]
                .map(item => clean(item.closest('label')?.innerText || item.value)).filter(Boolean);
            }
            return {
              selector: `[data-zhida-field="${marker}"]`, label,
              name: el.getAttribute('name') || '', field_type: (el.type || el.tagName).toLowerCase(),
              required: el.required || el.getAttribute('aria-required') === 'true' || /[*✱]/.test(label), options,
              current_value: el.type === 'file' ? [...(el.files || [])].map(file => file.name).join(', ') :
                (['checkbox','radio'].includes(el.type) ? String(el.checked) : String(el.value || '')),
              accept: el.getAttribute('accept') || ''
            };
          });
        }
        """)
        fields = [PageField.model_validate(item) for item in data]
        return BrowserSnapshot(session_id=self.session_id, url=self.page.url, title=await self.page.title(), fields=fields)

    async def snapshot_for(self, session_id: str) -> BrowserSnapshot:
        self._require(session_id)
        return await self.snapshot()

    async def pre_submit_check(self, session_id: str) -> PreSubmitCheck:
        page = self._require(session_id)
        data = await page.evaluate("""
        () => {
          const clean = value => String(value || '').replace(/\\s+/g, ' ').trim();
          const labelFor = el => {
            const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
            const question = el.closest('.application-question, .application-additional, fieldset, [role="group"], .form-field, .field');
            return clean(explicit?.innerText || question?.querySelector('.application-label, legend, .question-label')?.innerText ||
              el.closest('label')?.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.name);
          };
          const candidates = [...document.querySelectorAll('input, select, textarea')]
            .filter(el => !el.disabled && !['hidden','submit','button','image','reset','password'].includes((el.type || '').toLowerCase()))
            .filter(el => el.type === 'file' || el.getClientRects().length > 0);
          let filledCount = 0;
          let requiredTotal = 0;
          const missing = [];
          const handledRadioNames = new Set();
          for (const el of candidates) {
            const marker = el.getAttribute('data-zhida-field') || '';
            const label = labelFor(el).slice(0, 500);
            const required = el.required || el.getAttribute('aria-required') === 'true' || /[*✱]/.test(label);
            let filled = false;
            if (el.type === 'file') filled = Boolean(el.files?.length);
            else if (el.type === 'radio') {
              if (handledRadioNames.has(el.name)) continue;
              handledRadioNames.add(el.name);
              filled = [...document.querySelectorAll(`input[name="${CSS.escape(el.name)}"]`)].some(item => item.checked);
            } else if (el.type === 'checkbox') filled = el.checked;
            else filled = clean(el.value).length > 0;
            if (filled) filledCount += 1;
            if (required) {
              requiredTotal += 1;
              if (!filled) missing.push({selector: marker ? `[data-zhida-field="${marker}"]` : '', label, field_type: el.type || el.tagName.toLowerCase()});
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
            field_description = f"{field.label} {field.name}".lower() if field else ""
            deterministically_sensitive = any(hint in field_description for hint in SENSITIVE_FIELD_HINTS)
            compatible = bool(field) and (
                (action.action == "fill" and field.field_type not in {"checkbox", "radio", "select-one", "select-multiple"})
                or (action.action == "select" and field.field_type in {"select-one", "select-multiple"})
                or (action.action == "check" and field.field_type in {"checkbox", "radio"})
            )
            unsafe = (
                field is None
                or action.action not in {"fill", "select", "check"}
                or not compatible
                or (action.action in {"fill", "select"} and not str(action.value).strip())
                or (action.sensitive and not action.user_confirmed)
                or (deterministically_sensitive and not action.user_confirmed)
                or action.confidence < request.min_confidence
            )
            if unsafe:
                results.append(ActionResult(selector=action.selector, label=action.label, status="skipped",
                                            message="需要确认、敏感或置信度不足"))
                continue
            try:
                locator = page.locator(action.selector).first
                if action.action == "fill":
                    await locator.fill(str(action.value), timeout=8000)
                elif action.action == "select":
                    try:
                        await locator.select_option(label=str(action.value), timeout=8000)
                    except Exception:
                        await locator.select_option(value=str(action.value), timeout=8000)
                elif action.action == "check":
                    await locator.set_checked(bool(action.value), timeout=8000)
                if field.field_type in {"checkbox", "radio"}:
                    actual = str(await locator.is_checked()).lower()
                    expected = str(bool(action.value)).lower()
                    verified = actual.strip() == expected.strip()
                elif field.field_type in {"select-one", "select-multiple"}:
                    actual_value = await locator.input_value()
                    selected_text = (await locator.locator("option:checked").first.text_content() or "").strip()
                    actual = selected_text or actual_value
                    expected = str(action.value)
                    verified = expected.strip() in {actual_value.strip(), selected_text}
                else:
                    actual = await locator.input_value()
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
                locator = page.locator(result.selector).first
                expected = str(action.value)
                if field.field_type in {"checkbox", "radio"}:
                    result.actual_value = str(await locator.is_checked()).lower()
                    result.verified = result.actual_value == str(bool(action.value)).lower()
                elif field.field_type in {"select-one", "select-multiple"}:
                    actual_value = await locator.input_value()
                    selected_text = (await locator.locator("option:checked").first.text_content() or "").strip()
                    result.actual_value = selected_text or actual_value
                    result.verified = expected.strip() in {actual_value.strip(), selected_text}
                else:
                    result.actual_value = await locator.input_value()
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
