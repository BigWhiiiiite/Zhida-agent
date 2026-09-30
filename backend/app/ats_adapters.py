from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .application_models import ApplicationTarget, ApplicationWorkflowState, WorkflowAction
from .ats_registry import resolve_site_route, route_for_page
from .job_navigation import observe_navigation


FINAL_SUBMIT = re.compile(r"^(submit application|confirm application|确认投递|提交申请|提交简历|确认提交)$", re.I)
APPLY_START = re.compile(
    r"^(apply now|apply for this job|start application|立即投递|投递简历|申请职位|"
    r"立即申请|马上申请|去申请|申请)$", re.I,
)
VERIFY_BUTTON = re.compile(r"^(验证|确定|继续|下一步|登录|verify|continue|next|sign in)$", re.I)
REGISTER_BUTTON = re.compile(
    r"^(create account|sign up|register|create my account|创建账号|创建账户|注册|立即注册|注册并登录|"
    r"登录\s*[/／或]\s*注册|注册\s*[/／或]\s*登录)$", re.I,
)
SAFE_NEXT = re.compile(
    r"^(next|continue|save and continue|save & continue|next step|下一步|继续|保存并继续|保存并下一步|下一页)$", re.I,
)
JOB_CLOSED = re.compile(
    r"(?:该)?(?:职位|岗位)(?:已)?(?:下线|关闭|停止招聘|不存在|失效|删除|下架|结束招聘)|"
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
    return resolve_site_route(url).adapter


async def inspect_application_page(page: Page, session_id: str,
                                    target: ApplicationTarget | None = None) -> ApplicationWorkflowState:
    target = target or ApplicationTarget()
    navigation = await observe_navigation(page, target)
    data = await page.evaluate(r"""
    () => {
      const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
      const visible = el => el.getClientRects().length > 0 && !el.disabled &&
        getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
      const buttons = [...document.querySelectorAll('button, a, [role="button"], input[type="submit"], input[type="button"]')]
        .filter(visible).map(el => clean(el.innerText || el.value || el.getAttribute('aria-label'))).filter(Boolean);
      const inputSelector = 'input:not([type="hidden"]):not([type="button"]):not([type="submit"]),select,textarea';
      const ownedQuestion = el => {
        const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
          .map(id => document.getElementById(id)).filter(node => node && visible(node))
          .map(node => clean(node.innerText)).filter(Boolean).join(' ');
        if (labelledBy) return labelledBy;
        // Custom ATS labels are often sibling divs rather than <label for>.
        // Only inspect a short, single-control row; a section containing many
        // questions must not lend one question's title to another input.
        let row = el.parentElement;
        for (let depth = 0; row && depth < 4; depth++, row = row.parentElement) {
          if (row.matches('body,main,form,section,fieldset')) break;
          const fields = [...row.querySelectorAll(inputSelector)].filter(visible);
          if (fields.length !== 1 || fields[0] !== el || clean(row.innerText).length > 160) break;
          const labels = [...row.querySelectorAll('label,[class*="label" i],[class*="question-title" i]')]
            .filter(node => visible(node) && !node.contains(el) && !node.querySelector(inputSelector))
            .map(node => clean(node.innerText)).filter(value => value && value.length <= 60);
          if (new Set(labels).size === 1) return labels[0];
          const previous = (el.parentElement === row ? el : [...row.children].find(node => node.contains(el)))?.previousElementSibling;
          const value = clean(previous?.innerText);
          if (previous && visible(previous) && !previous.querySelector(inputSelector) && value && value.length <= 60)
            return value;
        }
        return '';
      };
      const inputs = [...document.querySelectorAll('input, select, textarea')].filter(visible).map(el => {
        const explicit = el.id ? document.querySelector(`label[for="${CSS.escape(el.id)}"]`) : null;
        const questionLabel = clean(explicit?.innerText || el.closest('label')?.innerText ||
          el.getAttribute('aria-label') || ownedQuestion(el));
        const label = questionLabel || clean(el.getAttribute('placeholder') || el.getAttribute('name'));
        return {type: (el.type || el.tagName).toLowerCase(), label, question_label: questionLabel, name: el.name || '',
          autocomplete: el.getAttribute('autocomplete') || '', checked: Boolean(el.checked)};
      });
      const bodyText = clean(document.body?.innerText).slice(0, 20000);
      const titleNode = document.querySelector('h1, .post_title, .post-name, .job-name, [class*="post-title" i], [class*="job-title" i], [class*="position-name" i]');
      const visibleTextNodes = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,legend,label,[role="heading"],span,p,div,dt')]
        .filter(node => visible(node) && !node.closest('nav,footer,[role="dialog"],[aria-modal="true"]'));
      const applicationTitles = visibleTextNodes.map(node => clean(node.innerText))
        .filter(value => value.length <= 240)
        .map(value => value.match(/^(?:您|你)?(?:正在|正)?(?:投递|申请)(?:的)?职位\s*[:：]\s*(.{2,200})$/)?.[1] || '')
        .filter(value => value && !/上传简历|个人信息|教育经历|加载中|暂无职位|未知职位/.test(value))
        .sort((a,b) => a.length - b.length);
      const shortLabels = [...new Set(visibleTextNodes.map(node => clean(node.innerText))
        .filter(value => value && value.length <= 40)
        .map(value => value.replace(/[\s*＊:：]/g, '')))];
      const formSections = shortLabels.filter(value => /^(个人信息|基本信息|联系方式|教育经历|教育背景|工作经历|实习经历|项目经历|投递意向|求职意向|上传简历)$/.test(value));
      const questionLabels = shortLabels.filter(value => /^(姓名|真实姓名|性别|出生日期|生日|邮箱|电子邮箱|手机号|手机号码|联系电话|学校|学校名称|院校名称|学历|最高学历|专业|专业名称)$/.test(value));
      const authModal = [...document.querySelectorAll(
        '[role="dialog"],[aria-modal="true"],dialog[open],.ant-modal,.el-dialog,.login-dialog,.loginDialog')]
        .filter(visible).some(dialog => {
          const content = clean(dialog.innerText);
          const fields = [...dialog.querySelectorAll('input')].filter(visible);
          const authField = fields.some(el => el.type === 'password' ||
            /one-time-code|otp|验证码|短信码|邮箱码/i.test([
              el.autocomplete, el.name, el.getAttribute('placeholder'), el.getAttribute('aria-label')].join(' ')));
          const authButton = [...dialog.querySelectorAll('button,a,[role="button"],input[type="submit"]')]
            .filter(visible).some(el => /^(登录|注册|立即登录|登录\s*[/／或]\s*注册|sign in|log in|register|sign up)$/i.test(
              clean(el.innerText || el.value || el.getAttribute('aria-label'))));
          return authField || (/登录|注册|sign in|log in|create account/i.test(content) &&
            (authButton || /扫码|二维码|scan.{0,10}code/i.test(content)));
        });
      const storageKeys = [];
      for (const storageName of ['localStorage', 'sessionStorage']) {
        try {
          const storage = window[storageName];
          for (let index = 0; index < storage.length; index += 1) {
            const key = storage.key(index) || '';
            if (key && storage.getItem(key)) storageKeys.push(key);
          }
        }
        catch (_) {}
      }
      return {body_text: bodyText, buttons, inputs, job_title: clean(titleNode?.innerText),
        application_job_title: new Set(applicationTitles).size === 1 ? applicationTitles[0] : '', form_sections: formSections,
        question_labels: questionLabels, auth_modal: authModal, storage_keys: storageKeys};
    }
    """)
    url = page.url
    site_route = await route_for_page(page)
    adapter = site_route.adapter
    parsed = urlparse(url)
    cookie_names = [item["name"] for item in await page.context.cookies([url]) if item.get("value")]
    text = data["body_text"]
    buttons: list[str] = data["buttons"]
    inputs: list[dict] = data["inputs"]
    otp = next((item for item in inputs if re.search(
        r"one-time-code|otp|verification|verify|验证码|校验码|短信码|邮箱码",
        f"{item['autocomplete']} {item['label']} {item['name']}", re.I)), None)
    password = any(item["type"] == "password" for item in inputs)
    register_button = next((button for button in buttons if REGISTER_BUTTON.match(button)), "")
    login_button = any(re.match(
        r"^(登录|sign in|log in|登录\s*[/／或]\s*注册|注册\s*[/／或]\s*登录)$", button, re.I,
    ) for button in buttons)
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
    authentication_evidence = [
        button for button in buttons
        if re.search(r"^(个人中心|我的申请|我的投递|投递记录|申请记录|我的简历|账号设置|退出登录|退出)$", button, re.I)
    ]
    if adapter == "moka-campus" and any(re.search(
        r"access.?token|auth.?token|candidate.?token|login.?token|(?:^|[_-])token(?:$|[_-])", key, re.I,
    ) for key in [*data.get("storage_keys", []), *cookie_names]):
        authentication_evidence.append("Moka 本机登录凭据")
    moka_application_fields = sum(bool(re.search(
        r"姓名|手机|电话|邮箱|学校|学历|专业|简历|name|mobile|phone|email|school|degree|resume",
        f"{item['type']} {item['label']} {item['name']} {item['autocomplete']}", re.I,
    )) for item in inputs)
    if (adapter == "moka-campus" and moka_application_fields >= 2 and not otp and not password and
            not login_button and not registration_page):
        authentication_evidence.append("已进入 Moka 申请表单")
    # A real active auth dialog overrides a background account menu or stale
    # session-token names. A global Login link by itself is not such evidence.
    active_auth_modal = bool(data.get("auth_modal"))
    if active_auth_modal:
        authentication_evidence = []
    authenticated = bool(authentication_evidence)
    search_fields = [item for item in inputs if item["type"] == "search" or re.search(
        r"搜索|关键词|关键字|search|keyword|搜索职位|搜索岗位", f"{item['label']} {item['name']}", re.I)]
    application_inputs = [item for item in inputs if item not in search_fields and
                          item["type"] not in {"hidden", "button", "submit"}]
    profile_evidence = sum(bool(re.search(
        r"姓名|邮箱|电子邮件|手机|电话|学校|学历|专业|经历|name|email|mobile|phone|school|degree|resume",
        f"{item['label']} {item['name']}", re.I)) for item in application_inputs)
    # Module/question captions plus real editable controls are independent
    # evidence that a custom SPA has reached its application form. A URL,
    # userId parameter, section menu or readonly title alone is never enough.
    structural_form_evidence = (len(application_inputs) >= 3 and
        len(data.get("question_labels", [])) >= 3 and
        (len(data.get("form_sections", [])) >= 2 or bool(data.get("application_job_title"))))
    safe_next_label = next((button for button in buttons
                            if SAFE_NEXT.match(button) and not FINAL_SUBMIT.match(button)), "")
    step_current, step_total = _step_progress(text)
    # Some genuine multi-page applications ask only one question per step.
    # Require an owned profile-question label (not an opaque name/placeholder),
    # visible bounded step progress and a known safe continuation control.
    owned_profile_question = any(
        item["type"] not in {"password", "checkbox", "hidden", "button", "submit"} and
        re.fullmatch(
            r"姓名|真实姓名|中文姓名|英文姓名|姓|名|邮箱|电子邮箱|电子邮件|手机号|手机号码|联系电话|"
            r"学校|学校名称|院校名称|学历|最高学历|专业|专业名称|性别|出生日期|生日|"
            r"(?:full |first |last )?name|e-?mail(?: address)?|(?:phone|mobile)(?: number)?|"
            r"school(?: name)?|degree|major|gender|date of birth",
            re.sub(r"^[\s*＊]+|[\s*＊:：]+$", "", item.get("question_label", "")), re.I,
        ) is not None for item in application_inputs
    )
    stepped_form_evidence = bool(
        owned_profile_question and safe_next_label and step_current is not None and
        step_total is not None and step_current < step_total and
        not (otp or password or registration_page or active_auth_modal)
    )
    form_evidence = (profile_evidence >= 2 or bool(application_inputs) and
                     bool(re.search(r"在线简历|申请表|填写简历|application form|application details", text, re.I)) or
                     structural_form_evidence or stepped_form_evidence)
    final_submit = any(FINAL_SUBMIT.match(button) for button in buttons) and form_evidence
    form_fields = len(application_inputs)
    registration_identifiers = []
    for name, pattern in (("email", r"email|邮箱|邮件"), ("phone", r"phone|mobile|手机|电话")):
        if any(re.search(pattern, f"{item['type']} {item['label']} {item['name']} {item['autocomplete']}", re.I)
               for item in inputs):
            registration_identifiers.append(name)

    apply_start_present = any(APPLY_START.match(button) for button in buttons)
    observed_job_id = navigation["job_id"]
    candidates = navigation["candidates"]
    beisen_detail_route = adapter == "beisen-italent" and bool(re.search(
        r"(?:^|/)detail(?:/|$)", f"{parsed.path}/{parsed.fragment}", re.I))
    application_route = bool(re.search(
        r"(?:^|/)(?:form|apply|application)(?:/|$)", f"{parsed.path}/{urlparse(parsed.fragment).path}", re.I))
    # jobAdId persists into /form?fromPage=job&jobAdId=... . It identifies the
    # job throughout the flow; it does not mean every subsequent page is JD.
    detail_url = (beisen_detail_route or bool(observed_job_id)) and not application_route
    explicit_job_heading = (bool(navigation["heading"]) and
        bool(re.search(r"岗位职责|工作职责|工作内容|任职要求|任职资格|职位描述|职位要求|岗位要求|"
                       r"job description|responsibilities|qualifications", text, re.I)))
    job_list_evidence = (not detail_url and not application_route and not explicit_job_heading and not form_evidence and (
        any(item.kind in {"open_job", "search_jobs"} for item in candidates) or
        bool(re.search(r"(?:#/jobs(?:\?|$)|/jobs/?$|/positions/?$)", url))))
    home_evidence = not detail_url and not application_route and not explicit_job_heading and not form_evidence and any(
        item.kind == "browse_jobs" for item in candidates)
    # A URL locator proves neither successful SPA loading nor a real job. Keep
    # the apply action unavailable until a visible title and job content exist.
    detail_evidence = explicit_job_heading
    readonly_review = not application_inputs and bool(re.search(
        r"确认申请|申请预览|简历预览|确认投递|核对申请|review.{0,20}application",
        navigation["heading"], re.I))
    moka_job_route = adapter == "moka-campus" and bool(re.search(r"(?:^|/)job/[^/?#]+", parsed.fragment, re.I))
    if JOB_CLOSED.search(text):
        stage, message = "unknown", "官方页面显示该职位已关闭或下线，已禁止进入申请。"
        actions = [WorkflowAction(intent="refresh", label="重新核验岗位", automated=True,
                                  requires_user=True)]
    elif readonly_review:
        stage, message = "unknown", "当前可能是只读申请预览，未检测到可核对表单；请人工检查，系统不会点击投递入口。"
        actions = [WorkflowAction(intent="refresh", label="重新核对页面", automated=True, requires_user=True)]
    elif (adapter == "tencent-campus" and parsed.path.endswith("/post_detail.html") and
          apply_start_present and not form_evidence and not active_auth_modal):
        stage, message = "job_detail", "已识别腾讯校招职位详情，可安全进入登录流程。"
        actions = [WorkflowAction(intent="start_application", label="进入腾讯登录", automated=True)]
    elif adapter == "tencent-campus" and parsed.path.endswith("/post_detail.html") and not active_auth_modal:
        stage, message = "unknown", "已打开腾讯职位页，但尚未识别到可用的投递按钮，请确认职位状态。"
        actions = [WorkflowAction(intent="refresh", label="重新核验岗位", automated=True,
                                  requires_user=True)]
    elif adapter == "tencent-campus" and parsed.path.endswith("/login.html"):
        stage = "auth_required"
        message = "腾讯校招使用 QQ/微信等社交账号登录，不是手机号注册。请在独立 Chrome 中亲自同意隐私政策并完成授权。"
        actions = [WorkflowAction(intent="manual_login", label="在 Chrome 中完成授权", requires_user=True),
                   WorkflowAction(intent="refresh", label="我已完成登录", automated=True)]
    elif registration_page and not authenticated:
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
    elif (password or active_auth_modal or (login_button and not detail_url and not application_route and
          not form_evidence and not apply_start_present and not (job_list_evidence or home_evidence))) and not authenticated:
        stage, message = "auth_required", ("当前登录弹窗要求登录或重新授权；背景中的职位详情不代表已完成登录。"
                                           if active_auth_modal else "页面需要用户登录或注册。")
        actions = [WorkflowAction(intent="manual_login", label="在 Chrome 中完成登录", requires_user=True),
                   WorkflowAction(intent="refresh", label="我已完成登录", automated=True)]
    elif moka_job_route and apply_start_present and not form_evidence:
        stage = "job_detail"
        message = ("已识别 Moka 登录状态和职位申请入口，可进入申请表单。" if authenticated else
                   "已识别 Moka 职位详情；进入申请后如需登录，请在职达专用 Chrome 中完成。")
        actions = [WorkflowAction(intent="start_application", label="进入 Moka 申请流程", automated=True)]
    elif moka_job_route and login_button and not authenticated:
        stage, message = "auth_required", "Moka 当前仍显示登录入口，请在职达专用 Chrome 中完成登录。"
        actions = [WorkflowAction(intent="manual_login", label="在 Chrome 中完成登录", requires_user=True),
                   WorkflowAction(intent="refresh", label="我已完成登录", automated=True)]
    elif detail_url and not explicit_job_heading and not form_evidence:
        stage = "unknown"
        message = ("已识别具体岗位详情链接，但尚未读取到完整的岗位标题和职责正文。"
                   "当前搜索框可能只是全站导航，不代表岗位列表；请等待或重新加载招聘网页后点击重新识别。"
                   "系统不会仅凭链接进入申请或填写资料。")
        actions = [WorkflowAction(intent="refresh", label="重新识别已加载的岗位详情", automated=True,
                                  requires_user=True)]
    elif application_route and not form_evidence:
        stage = "unknown"
        message = ("当前网址位于申请流程，但尚未检测到可填写的个人资料表单。"
                   "请等待表单加载或完成页面提示后重新识别；不会仅凭网址、用户标识或只读标题开始填写。")
        actions = [WorkflowAction(intent="refresh", label="重新识别申请表", automated=True,
                                  requires_user=True)]
    elif job_list_evidence:
        stage, message = "job_list", "当前是岗位列表或筛选页面；请先找到并选择具体岗位，不会填写个人资料。"
        actions = [WorkflowAction(intent="open_job", label="选择已识别岗位", automated=True, requires_user=True)]
        if any(item.kind == "search_jobs" for item in candidates):
            actions.insert(0, WorkflowAction(intent="search_jobs", label="搜索目标岗位", automated=True))
    elif home_evidence:
        stage, message = "homepage", "当前是招聘首页或栏目入口，尚未选择具体岗位；先进入岗位列表。"
        actions = [WorkflowAction(intent="browse_jobs", label="浏览招聘岗位", automated=True)]
    elif apply_start_present and detail_evidence and not form_evidence:
        stage, message = "job_detail", "已在官方职位页识别到申请入口，可进入登录或申请流程。"
        actions = [WorkflowAction(intent="start_application", label="进入申请流程", automated=True)]
    elif final_submit and not safe_next_label:
        stage, message = "review", "已到达最终提交页；系统已停止自动翻页，等待你人工终审。"
        actions = [WorkflowAction(intent="refresh", label="重新检查最终页", automated=True,
                                  requires_user=True)]
    elif form_fields and not (job_list_evidence or home_evidence):
        stage = "review" if final_submit and not safe_next_label else "application_form"
        message = ("已到达最终提交页，可继续核对和补填；系统不会点击最终提交。" if stage == "review" else
                   "已识别可填写的在线简历或申请表，可进入字段分析。")
        actions = [WorkflowAction(intent="analyze_form", label="分析当前页表单", automated=True)]
        if safe_next_label:
            actions.append(WorkflowAction(intent="continue_application",
                                          label=f"检查当前页后点击“{safe_next_label}”", automated=True))
    elif detail_url or explicit_job_heading:
        stage = "unknown"
        message = ("已读取岗位标题和职责正文，但尚未识别到可用的申请入口。"
                   "请核对职位是否仍开放，或等待页面加载后重新识别；不会将全站搜索当作岗位列表。")
        actions = [WorkflowAction(intent="refresh", label="重新核验岗位入口", automated=True,
                                  requires_user=True)]
    else:
        stage = "unknown"
        message = ("已识别登录状态，但当前页还没有出现申请表单；请等待页面加载或进入具体申请页。"
                   if authenticated else "暂时无法判定当前页面阶段，可在 Chrome 中导航后重新识别。")
        actions = [WorkflowAction(intent="refresh", label="重新识别当前页", automated=True)]

    job_id = observed_job_id or parse_qs(parsed.query).get("postid", [""])[0]
    if not job_id and adapter == "moka-campus":
        match = re.search(r"(?:^|/)job/([^/?#]+)", parsed.fragment, re.I)
        job_id = match.group(1) if match else ""
    return ApplicationWorkflowState(
        session_id=session_id, url=url, title=await page.title(), adapter=adapter,
        site_route=site_route, stage=stage,
        message=message, job_title=(data.get("application_job_title") or
                                  (navigation["heading"] if adapter == "beisen-italent" else
                                   navigation["heading"] or data["job_title"])), job_id=job_id,
        authenticated=authenticated, authentication_evidence=list(dict.fromkeys(authentication_evidence)),
        authentication_methods=methods, requires_consent=unchecked_consent,
        verification_channel=("email" if otp and re.search(r"email|邮箱", f"{otp['label']} {otp['name']}", re.I)
                              else "sms" if otp else ""),
        registration_identifiers=registration_identifiers,
        registration_requires_password=password,
        form_fields=form_fields, final_submit_present=final_submit,
        safe_next_present=bool(safe_next_label), safe_next_label=safe_next_label,
        page_step_current=step_current, page_step_total=step_total, actions=actions,
        target=target, navigation_candidates=candidates,
        stage_evidence=([f"链接中的岗位标识（不代表详情已加载）：{job_id}"] if job_id else []) +
            (["已读取可见岗位标题及职责/要求正文"] if explicit_job_heading else []) +
            (["详情链接尚缺可核对的岗位正文"] if detail_url and not explicit_job_heading and not form_evidence else []) +
            (["已观察到岗位列表/搜索控件"] if job_list_evidence else []) +
            (["已观察到招聘栏目入口，未识别具体岗位"] if home_evidence else []) +
            (["已读取申请页的个人资料模块及题目标题"] if structural_form_evidence else []) +
            (["已识别明确步骤进度、个人资料题目及安全下一步"] if stepped_form_evidence else []) +
            ([f"已识别申请表资料控件：{form_fields} 项"] if form_evidence else []),
    )


async def start_application(page: Page) -> None:
    state = await inspect_application_page(page, "check")
    if state.stage != "job_detail":
        raise ValueError("只能从已识别的职位详情页进入申请流程")
    candidates = page.locator("button, a, [role=button]")
    click_errors: list[str] = []
    for index in range(await candidates.count()):
        item = candidates.nth(index)
        if not await item.is_visible():
            continue
        text = re.sub(r"\s+", " ", (await item.inner_text()).strip())
        if APPLY_START.match(text):
            try:
                await item.click(timeout=4000)
            except PlaywrightTimeoutError as exc:
                # Some Moka job pages render an overlapping recommendation panel over the
                # visible apply button. The text has already passed the strict allow-list,
                # so a DOM click is still a bounded navigation action, never a submit action.
                click_errors.append(str(exc).splitlines()[0])
                try:
                    await item.evaluate("element => element.click()")
                except Exception as fallback_exc:
                    click_errors.append(str(fallback_exc).splitlines()[0])
                    continue
            await page.wait_for_timeout(1500)
            return
    detail = f"（{click_errors[-1]}）" if click_errors else ""
    raise LookupError(f"未找到可安全点击的“开始申请”按钮{detail}")


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
    final_control_seen = False
    for index in range(await buttons.count()):
        item = buttons.nth(index)
        if not await item.is_visible() or await item.is_disabled():
            continue
        text = ((await item.inner_text()) or await item.get_attribute("value") or "").strip()
        final_control_seen = final_control_seen or bool(FINAL_SUBMIT.match(text))
        if SAFE_NEXT.match(text) and not FINAL_SUBMIT.match(text):
            await item.click()
            await page.wait_for_timeout(1200)
            return
    if final_control_seen:
        raise ValueError("当前只剩最终提交按钮，系统已停止自动操作")
    raise LookupError("未找到可安全点击的“下一步/继续”按钮")
