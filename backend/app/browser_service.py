from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from .browser_models import ActionResult, BrowserSnapshot, ExecutePlanRequest, ExecutionResult, PageField


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
        return self.page

    async def snapshot(self) -> BrowserSnapshot:
        if not self.page or not self.session_id:
            raise LookupError("浏览器会话尚未启动")
        data = await self.page.evaluate("""
        () => {
          const elements = [...document.querySelectorAll('input, select, textarea')]
            .filter(el => !['hidden','submit','button','image','reset','password','file'].includes((el.type || '').toLowerCase()))
            .filter(el => !el.disabled && el.getClientRects().length > 0);
          return elements.map((el, index) => {
            const marker = el.getAttribute('data-zhida-field') ||
              `zhida-${Date.now()}-${index}-${Math.random().toString(36).slice(2)}`;
            el.setAttribute('data-zhida-field', marker);
            const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
            const wrapping = el.closest('label');
            const label = (explicit?.innerText || wrapping?.innerText || el.getAttribute('aria-label') ||
              el.getAttribute('placeholder') || el.getAttribute('name') || '').trim().slice(0, 300);
            const options = el.tagName === 'SELECT' ? [...el.options].map(o => o.text.trim()).filter(Boolean) : [];
            return {
              selector: `[data-zhida-field="${marker}"]`, label,
              name: el.getAttribute('name') || '', field_type: (el.type || el.tagName).toLowerCase(),
              required: el.required || el.getAttribute('aria-required') === 'true', options,
              current_value: ['checkbox','radio'].includes(el.type) ? String(el.checked) : String(el.value || '')
            };
          });
        }
        """)
        fields = [PageField.model_validate(item) for item in data]
        return BrowserSnapshot(session_id=self.session_id, url=self.page.url, title=await self.page.title(), fields=fields)

    async def snapshot_for(self, session_id: str) -> BrowserSnapshot:
        self._require(session_id)
        return await self.snapshot()

    async def execute(self, session_id: str, request: ExecutePlanRequest) -> ExecutionResult:
        page = self._require(session_id)
        snapshot = await self.snapshot()
        fields = {field.selector: field for field in snapshot.fields}
        results: list[ActionResult] = []
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
                or action.sensitive
                or deterministically_sensitive
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
                results.append(ActionResult(selector=action.selector, label=action.label, status="filled"))
            except Exception as exc:
                results.append(ActionResult(selector=action.selector, label=action.label, status="failed",
                                            message=str(exc)[:240]))
        await page.wait_for_timeout(500)
        return ExecutionResult(url=page.url, completed=sum(r.status == "filled" for r in results),
                               skipped=sum(r.status == "skipped" for r in results),
                               failed=sum(r.status == "failed" for r in results), results=results)

    async def close(self) -> None:
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
        self.playwright = None; self.browser = None; self.context = None; self.page = None; self.session_id = None


browser_demo = BrowserDemoService()
