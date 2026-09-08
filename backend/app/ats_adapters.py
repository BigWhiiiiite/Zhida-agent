from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page

from .application_models import ApplicationWorkflowState, WorkflowAction


FINAL_SUBMIT = re.compile(r"^(submit application|confirm application|确认投递|提交申请|提交简历|确认提交)$", re.I)
APPLY_START = re.compile(r"^(apply now|apply for this job|start application|立即投递|投递简历|申请职位)$", re.I)
VERIFY_BUTTON = re.compile(r"^(验证|确定|继续|下一步|登录|verify|continue|next|sign in)$", re.I)


def adapter_name(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host == "join.qq.com" or host.endswith(".join.qq.com"):
        return "tencent-campus"
    if "lever.co" in host:
        return "lever"
    if "greenhouse.io" in host:
        return "greenhouse"
    if "myworkdayjobs.com" in host:
        return "workday"
    return "generic"


async def inspect_application_page(page: Page, session_id: str) -> ApplicationWorkflowState:
    data = await page.evaluate(r"""
    () => {
      const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
      const visible = el => el.getClientRects().length > 0 && !el.disabled;
      const buttons = [...document.querySelectorAll('button, a, [role="button"], input[type="submit"], input[type="button"]')]
        .filter(visible).map(el => clean(el.innerText || el.value || el.getAttribute('aria-label'))).filter(Boolean);
      const inputs = [...document.querySelectorAll('input, select, textarea')].filter(visible).map(el => {
        const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
        const label = clean(explicit?.innerText || el.closest('label')?.innerText || el.getAttribute('aria-label') ||
          el.getAttribute('placeholder') || el.getAttribute('name'));
        return {type: (el.type || el.tagName).toLowerCase(), label, name: el.name || '',
          autocomplete: el.getAttribute('autocomplete') || '', checked: Boolean(el.checked)};
      });
      const bodyText = clean(document.body?.innerText).slice(0, 20000);
      const titleNode = document.querySelector('h1, .post_title, .post-name, [class*="post-title" i], [class*="job-title" i]');
      return {body_text: bodyText, buttons, inputs, job_title: clean(titleNode?.innerText)};
    }
    """)
    url = page.url
    adapter = adapter_name(url)
    parsed = urlparse(url)
    text = data["body_text"]
    buttons: list[str] = data["buttons"]
    inputs: list[dict] = data["inputs"]
    otp = next((item for item in inputs if re.search(
        r"one-time-code|otp|verification|verify|验证码|校验码|短信码|邮箱码",
        f"{item['autocomplete']} {item['label']} {item['name']}", re.I)), None)
    password = any(item["type"] == "password" for item in inputs)
    unchecked_checkbox = any(item["type"] == "checkbox" and not item["checked"] for item in inputs)
    unchecked_consent = any(
        item["type"] == "checkbox" and not item["checked"] and
        re.search(r"同意|隐私|条款|consent|privacy|terms", item["label"], re.I)
        for item in inputs
    ) or bool(unchecked_checkbox and re.search(r"我已阅读并同意|隐私政策|privacy policy|terms", text, re.I))
    methods = [name for name, pattern in (
        ("QQ", r"QQ账号登录"), ("微信", r"微信账号登录"),
        ("LinkedIn", r"LinkedIn"), ("Google", r"Google"),
        ("手机验证码", r"手机|短信"), ("邮箱验证码", r"邮箱|email"),
    ) if re.search(pattern, text, re.I)]
    final_submit = any(FINAL_SUBMIT.match(button) for button in buttons)
    form_fields = sum(item["type"] not in {"hidden", "button", "submit"} for item in inputs)

    if adapter == "tencent-campus" and parsed.path.endswith("/post_detail.html"):
        stage, message = "job_detail", "已识别腾讯校招职位详情，可安全进入登录流程。"
        actions = [WorkflowAction(intent="start_application", label="进入腾讯登录", automated=True)]
    elif adapter == "tencent-campus" and parsed.path.endswith("/login.html"):
        stage = "auth_required"
        message = "腾讯校招使用 QQ/微信等社交账号登录，不是手机号注册。请在独立 Chrome 中亲自同意隐私政策并完成授权。"
        actions = [WorkflowAction(intent="manual_login", label="在 Chrome 中完成授权", requires_user=True),
                   WorkflowAction(intent="refresh", label="我已完成登录", automated=True)]
    elif otp:
        stage = "verification_required"
        channel = "email" if re.search(r"email|邮箱", f"{otp['label']} {otp['name']}", re.I) else "sms"
        message = "页面正在等待验证码。验证码只用于当前浏览器会话，不写入数据库或日志。"
        request_intent = "request_email_code" if channel == "email" else "request_phone_code"
        actions = [WorkflowAction(intent=request_intent, label="获取验证码", automated=True),
                   WorkflowAction(intent="enter_verification", label="输入验证码并继续", automated=True,
                                  requires_user=True)]
    elif password or re.search(r"登录|sign in|log in", text, re.I):
        stage, message = "auth_required", "页面需要用户登录或注册。"
        actions = [WorkflowAction(intent="manual_login", label="在 Chrome 中完成登录", requires_user=True),
                   WorkflowAction(intent="refresh", label="我已完成登录", automated=True)]
    elif form_fields:
        stage = "review" if final_submit and re.search(r"预览|确认|review", text, re.I) else "application_form"
        message = "已识别可填写的在线简历或申请表，可进入字段分析。"
        actions = [WorkflowAction(intent="analyze_form", label="分析当前页表单", automated=True)]
    else:
        stage, message = "unknown", "暂时无法判定当前页面阶段，可在 Chrome 中导航后重新识别。"
        actions = [WorkflowAction(intent="refresh", label="重新识别当前页", automated=True)]

    job_id = parse_qs(parsed.query).get("postid", [""])[0]
    return ApplicationWorkflowState(
        session_id=session_id, url=url, title=await page.title(), adapter=adapter, stage=stage,
        message=message, job_title=data["job_title"], job_id=job_id,
        authentication_methods=methods, requires_consent=unchecked_consent,
        verification_channel=("email" if otp and re.search(r"email|邮箱", f"{otp['label']} {otp['name']}", re.I)
                              else "sms" if otp else ""),
        form_fields=form_fields, final_submit_present=final_submit, actions=actions,
    )


async def start_application(page: Page) -> None:
    state = await inspect_application_page(page, "check")
    if state.stage != "job_detail":
        raise ValueError("只能从已识别的职位详情页进入申请流程")
    candidates = page.locator("button, a, [role=button]")
    for index in range(await candidates.count()):
        item = candidates.nth(index)
        if not await item.is_visible():
            continue
        text = (await item.inner_text()).strip()
        if APPLY_START.match(text):
            await item.click()
            await page.wait_for_timeout(1200)
            return
    raise LookupError("未找到可安全点击的“开始申请”按钮")


async def fill_verification_code(page: Page, code: str, submit: bool) -> None:
    inputs = page.locator("input, textarea")
    target = None
    for index in range(await inputs.count()):
        item = inputs.nth(index)
        if not await item.is_visible():
            continue
        description = " ".join(filter(None, [
            await item.get_attribute("autocomplete"), await item.get_attribute("placeholder"),
            await item.get_attribute("name"), await item.get_attribute("aria-label"),
        ]))
        if re.search(r"one-time-code|otp|verification|verify|验证码|校验码|短信码|邮箱码", description, re.I):
            target = item
            break
    if target is None:
        raise LookupError("当前页面没有找到验证码输入框")
    await target.fill(code)
    if not submit:
        return
    buttons = page.locator("button, [role=button], input[type=submit], input[type=button]")
    for index in range(await buttons.count()):
        item = buttons.nth(index)
        if not await item.is_visible() or await item.is_disabled():
            continue
        text = ((await item.inner_text()) or await item.get_attribute("value") or "").strip()
        if VERIFY_BUTTON.match(text) and not FINAL_SUBMIT.match(text):
            await item.click()
            await page.wait_for_timeout(1200)
            return
    raise LookupError("验证码已填入，但未找到可安全点击的验证按钮")


async def request_verification_code(page: Page, channel: str, value: str) -> None:
    if not value:
        raise ValueError("主档案中没有可用的手机号或邮箱")
    keyword = r"phone|mobile|手机|电话" if channel == "phone" else r"email|邮箱|邮件"
    inputs = page.locator("input")
    target = None
    for index in range(await inputs.count()):
        item = inputs.nth(index)
        if not await item.is_visible():
            continue
        description = " ".join(filter(None, [await item.get_attribute("type"), await item.get_attribute("name"),
                                               await item.get_attribute("placeholder"), await item.get_attribute("aria-label")]))
        if re.search(keyword, description, re.I):
            target = item
            break
    if target is None:
        raise LookupError("未找到对应的手机号或邮箱输入框")
    await target.fill(value)
    buttons = page.locator("button, [role=button], input[type=button]")
    for index in range(await buttons.count()):
        item = buttons.nth(index)
        if not await item.is_visible() or await item.is_disabled():
            continue
        text = ((await item.inner_text()) or await item.get_attribute("value") or "").strip()
        if re.search(r"(获取|发送).*(验证码|校验码)|send code|get code", text, re.I):
            await item.click()
            await page.wait_for_timeout(1000)
            return
    raise LookupError("联系方式已填入，但未找到发送验证码按钮")
