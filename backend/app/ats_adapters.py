from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Locator, Page

from .application_models import ApplicationWorkflowState, WorkflowAction


FINAL_SUBMIT = re.compile(r"^(submit application|confirm application|确认投递|提交申请|提交简历|确认提交)$", re.I)
APPLY_START = re.compile(r"^(apply now|apply for this job|start application|立即投递|投递简历|申请职位)$", re.I)
VERIFY_BUTTON = re.compile(r"^(验证|确定|继续|下一步|登录|verify|continue|next|sign in)$", re.I)
REGISTER_BUTTON = re.compile(
    r"^(create account|sign up|register|create my account|创建账号|创建账户|注册|立即注册|注册并登录)$", re.I,
)
SAFE_NEXT = re.compile(
    r"^(next|continue|save and continue|save & continue|next step|下一步|继续|保存并继续|保存并下一步|下一页)$", re.I,
)
JOB_CLOSED = re.compile(
    r"该职位(?:已)?(?:下线|关闭|停止招聘)|职位不存在|职位已失效|"
    r"position (?:has been |is )?closed|job (?:is )?no longer available", re.I,
)


def _step_progress(text: str) -> tuple[int | None, int | None]:
    for pattern in (
        r"\bstep\s*(\d{1,2})\s*(?:of|/)\s*(\d{1,2})\b",
        r"第\s*(\d{1,2})\s*步.{0,20}?共\s*(\d{1,2})\s*步",
        r"\b(\d{1,2})\s*/\s*(\d{1,2})\s*(?:steps?|步骤)",
    ):
        match = re.search(pattern, text, re.I)
        if match:
            current, total = int(match.group(1)), int(match.group(2))
            if 1 <= current <= total <= 20:
                return current, total
    return None, None


async def _input_description(item: Locator) -> str:
    return await item.evaluate(r"""
    el => {
      const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
      const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
      return clean([
        el.type, el.name, el.getAttribute('autocomplete'), el.getAttribute('placeholder'),
        el.getAttribute('aria-label'), explicit?.innerText, el.closest('label')?.innerText
      ].filter(Boolean).join(' '));
    }
    """)


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
    register_button = next((button for button in buttons if REGISTER_BUTTON.match(button)), "")
    login_button = any(re.match(r"^(登录|sign in|log in)$", button, re.I) for button in buttons)
    password_inputs = [item for item in inputs if item["type"] == "password"]
    registration_password = len(password_inputs) >= 2 or any(re.search(
        r"new-password|confirm|确认|再次", f"{item['autocomplete']} {item['label']} {item['name']}", re.I
    ) for item in password_inputs)
    registration_heading = bool(re.search(
        r"创建账(?:号|户)|注册新账(?:号|户)|create (?:an? )?account|sign up|register now",
        data["job_title"], re.I,
    ))
    registration_page = registration_password or bool(
        register_button and ((not login_button and (password or otp)) or registration_heading)
    )
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
    safe_next_label = next((button for button in buttons
                            if SAFE_NEXT.match(button) and not FINAL_SUBMIT.match(button)), "")
    form_fields = sum(item["type"] not in {"hidden", "button", "submit"} for item in inputs)
    registration_identifiers = []
    for name, pattern in (("email", r"email|邮箱|邮件"), ("phone", r"phone|mobile|手机|电话")):
        if any(re.search(pattern, f"{item['type']} {item['label']} {item['name']} {item['autocomplete']}", re.I)
               for item in inputs):
            registration_identifiers.append(name)
    step_current, step_total = _step_progress(text)

    apply_start_present = any(APPLY_START.match(button) for button in buttons)
    if JOB_CLOSED.search(text):
        stage, message = "unknown", "官方页面显示该职位已关闭或下线，已禁止进入申请。"
        actions = [WorkflowAction(intent="refresh", label="重新核验岗位", automated=True,
                                  requires_user=True)]
    elif adapter == "tencent-campus" and parsed.path.endswith("/post_detail.html") and apply_start_present:
        stage, message = "job_detail", "已识别腾讯校招职位详情，可安全进入登录流程。"
        actions = [WorkflowAction(intent="start_application", label="进入腾讯登录", automated=True)]
    elif adapter == "tencent-campus" and parsed.path.endswith("/post_detail.html"):
        stage, message = "unknown", "已打开腾讯职位页，但尚未识别到可用的投递按钮，请确认职位状态。"
        actions = [WorkflowAction(intent="refresh", label="重新核验岗位", automated=True,
                                  requires_user=True)]
    elif adapter == "tencent-campus" and parsed.path.endswith("/login.html"):
        stage = "auth_required"
        message = "腾讯校招使用 QQ/微信等社交账号登录，不是手机号注册。请在独立 Chrome 中亲自同意隐私政策并完成授权。"
        actions = [WorkflowAction(intent="manual_login", label="在 Chrome 中完成授权", requires_user=True),
                   WorkflowAction(intent="refresh", label="我已完成登录", automated=True)]
    elif registration_page:
        stage = "registration_required"
        message = "已识别创建账号页面。可以填入联系方式和一次性密码；隐私协议由你亲自确认，账号密码不会写入智达数据库。"
        actions = [WorkflowAction(intent="fill_registration", label="填入注册信息", automated=True,
                                  requires_user=True),
                   WorkflowAction(intent="create_account", label="我已核对，创建账号", automated=True,
                                  requires_user=True)]
        if otp:
            request_intent = "request_email_code" if re.search(
                r"email|邮箱", f"{otp['label']} {otp['name']}", re.I) else "request_phone_code"
            actions[0:0] = [WorkflowAction(intent=request_intent, label="获取验证码", automated=True),
                            WorkflowAction(intent="enter_verification", label="输入验证码", automated=True,
                                           requires_user=True)]
    elif otp:
        stage = "verification_required"
        channel = "email" if re.search(r"email|邮箱", f"{otp['label']} {otp['name']}", re.I) else "sms"
        message = "页面正在等待验证码。验证码只用于当前浏览器会话，不写入数据库或日志。"
        request_intent = "request_email_code" if channel == "email" else "request_phone_code"
        actions = [WorkflowAction(intent=request_intent, label="获取验证码", automated=True),
                   WorkflowAction(intent="enter_verification", label="输入验证码并继续", automated=True,
                                  requires_user=True)]
    elif password or (login_button and not apply_start_present):
        stage, message = "auth_required", "页面需要用户登录或注册。"
        actions = [WorkflowAction(intent="manual_login", label="在 Chrome 中完成登录", requires_user=True),
                   WorkflowAction(intent="refresh", label="我已完成登录", automated=True)]
    elif apply_start_present:
        stage, message = "job_detail", "已在官方职位页识别到申请入口，可进入登录或申请流程。"
        actions = [WorkflowAction(intent="start_application", label="进入申请流程", automated=True)]
    elif final_submit and not safe_next_label:
        stage, message = "review", "已到达最终提交页；系统已停止自动翻页，等待你人工终审。"
        actions = [WorkflowAction(intent="refresh", label="重新检查最终页", automated=True,
                                  requires_user=True)]
    elif form_fields:
        stage = "review" if final_submit and not safe_next_label else "application_form"
        message = ("已到达最终提交页，可继续核对和补填；系统不会点击最终提交。" if stage == "review" else
                   "已识别可填写的在线简历或申请表，可进入字段分析。")
        actions = [WorkflowAction(intent="analyze_form", label="分析当前页表单", automated=True)]
        if safe_next_label:
            actions.append(WorkflowAction(intent="continue_application",
                                          label=f"检查当前页后点击“{safe_next_label}”", automated=True))
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
        registration_identifiers=registration_identifiers,
        registration_requires_password=password,
        form_fields=form_fields, final_submit_present=final_submit,
        safe_next_present=bool(safe_next_label), safe_next_label=safe_next_label,
        page_step_current=step_current, page_step_total=step_total, actions=actions,
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
        description = await _input_description(item)
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
        description = await _input_description(item)
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


async def fill_registration_info(page: Page, email: str, phone: str, password: str) -> None:
    """Fill account data without accepting agreements or creating the account."""
    inputs = page.locator("input")
    password_targets = []
    identifier_filled = 0
    for index in range(await inputs.count()):
        item = inputs.nth(index)
        if not await item.is_visible() or await item.is_disabled():
            continue
        description = await _input_description(item)
        input_type = (await item.get_attribute("type") or "text").lower()
        if input_type == "password" or re.search(r"password|密码", description, re.I):
            password_targets.append(item)
        elif email and re.search(r"email|邮箱|邮件", description, re.I):
            await item.fill(email)
            identifier_filled += 1
        elif phone and re.search(r"phone|mobile|tel|手机|电话", description, re.I):
            await item.fill(phone)
            identifier_filled += 1
    if not identifier_filled:
        raise LookupError("当前页面没有找到可匹配的邮箱或手机号输入框")
    if password_targets and len(password) < 8:
        raise ValueError("当前注册页需要密码，请输入至少 8 位")
    for target in password_targets:
        await target.fill(password)


async def create_account(page: Page) -> None:
    """Create an account only after an explicit user action and strict safety checks."""
    state = await inspect_application_page(page, "registration-check")
    if state.stage != "registration_required":
        raise ValueError("当前页面不是可识别的注册页")
    if state.requires_consent:
        raise ValueError("请先在 Chrome 中阅读并亲自勾选隐私协议")
    challenge = page.locator(
        '.h-captcha, .g-recaptcha, [data-sitekey], iframe[src*="captcha" i], [class*="turnstile" i]'
    )
    if await challenge.count():
        raise ValueError("请先在 Chrome 中完成人机验证")
    readiness = await page.evaluate("""
    () => {
      const visible = el => el.getClientRects().length > 0 && !el.disabled;
      const passwords = [...document.querySelectorAll('input[type="password"]')].filter(visible);
      const identities = [...document.querySelectorAll('input[type="email"], input[type="tel"], input[name*="email" i], input[name*="phone" i], input[name*="mobile" i]')].filter(visible);
      const missingRequired = [...document.querySelectorAll('input[required], select[required], textarea[required]')]
        .filter(visible).some(el => el.type === 'checkbox' ? !el.checked : !String(el.value || '').trim());
      return {password: !passwords.length || passwords.some(el => el.value.length >= 8), identity: identities.some(el => String(el.value || '').trim()), missingRequired};
    }
    """)
    if not readiness["password"] or not readiness["identity"] or readiness["missingRequired"]:
        raise ValueError("注册信息尚未填完，请先填入并检查必填项")
    buttons = page.locator("button, [role=button], input[type=submit], input[type=button]")
    for index in range(await buttons.count()):
        item = buttons.nth(index)
        if not await item.is_visible() or await item.is_disabled():
            continue
        text = ((await item.inner_text()) or await item.get_attribute("value") or "").strip()
        if REGISTER_BUTTON.match(text) and not FINAL_SUBMIT.match(text):
            await item.click()
            await page.wait_for_timeout(1200)
            return
    raise LookupError("未找到可安全点击的“创建账号”按钮")


async def continue_application(page: Page) -> None:
    """Advance one non-final application step. Final submit labels are never clicked."""
    buttons = page.locator("button, a, [role=button], input[type=submit], input[type=button]")
    for index in range(await buttons.count()):
        item = buttons.nth(index)
        if not await item.is_visible() or await item.is_disabled():
            continue
        text = ((await item.inner_text()) or await item.get_attribute("value") or "").strip()
        if SAFE_NEXT.match(text) and not FINAL_SUBMIT.match(text):
            await item.click()
            await page.wait_for_timeout(1200)
            return
    if (await inspect_application_page(page, "continue-check")).final_submit_present:
        raise ValueError("当前只剩最终提交按钮，系统已停止自动操作")
    raise LookupError("未找到可安全点击的“下一步/继续”按钮")
