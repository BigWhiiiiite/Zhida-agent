from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from .application_models import ApplicationTarget, ApplicationWorkflowState, WorkflowAction
from .ats_registry import resolve_site_route, route_for_page
from .job_navigation import observe_navigation
from .autohome_confirmation import enter_selected_application, has_entry_confirmation
from .autohome_entry import inspect_autohome_account


FINAL_SUBMIT = re.compile(r"^(submit application|confirm application|确认投递|提交申请|提交简历|确认提交)$", re.I)
APPLY_START = re.compile(
    r"^(apply now|apply for this job|start application|立即投递|投递简历|申请职位|"
    r"立即申请|马上申请|去申请|申请|申请该职位)$", re.I,
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


def _custom_detail_route(url: str) -> bool:
    parsed = urlparse(url)
    return bool(re.fullmatch(r"/custom/[^/]*detail/?", parsed.path, re.I))


async def _custom_detail_entry(page: Page, navigation: dict) -> str:
    """Bound a non-semantic ATS CTA to a loaded, uniquely observed JD.

    An ID or route is not click authority. The current visible title, a JD
    section in the same bounded container, exact entry wording and absence of
    application controls are independent requirements. Never include final
    submit labels, generic divs, hidden/stale/duplicate IDs or recommendation
    cards. No target or document title is used as the missing DOM evidence.
    """
    if (adapter_name(page.url) != "beisen-italent" or not _custom_detail_route(page.url) or
            not navigation.get("heading") or not navigation.get("detail_content")):
        return ""
    return await page.evaluate(r"""heading => {
      const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
      const visible=el=>el&&el.getClientRects().length>0&&!el.disabled&&
        getComputedStyle(el).visibility!=='hidden'&&getComputedStyle(el).display!=='none';
      const entries=[...document.querySelectorAll('[id="apply"]')];
      if(entries.length!==1) return '';
      const entry=entries[0];
      if(!visible(entry)||entry.getAttribute('aria-disabled')==='true'||
         entry.closest('form,nav,header,footer,[role="dialog"],[aria-modal="true"],[class*="recommend" i]')||
         entry.matches('input[type="submit"],button[type="submit"]')||
         !/^(立即申请|申请职位|申请该职位|马上申请|去申请|apply now|apply for this job|start application)$/i.test(clean(entry.innerText))) return '';
      // A preview/application page with a reused #apply is never a JD entry.
      const controls=[...document.querySelectorAll('input,select,textarea')].filter(visible)
        .filter(el=>!el.matches('[type="hidden"],[type="search"],[type="button"],[type="submit"]')&&
          !/搜索|关键词|关键字|search|keyword/i.test([el.placeholder,el.name,el.getAttribute('aria-label')].join(' ')));
      if(controls.length) return '';
      let region=entry.parentElement;
      for(let depth=0;region&&depth<6;depth++,region=region.parentElement){
        if(region.matches('body,html,nav,header,footer,form')) break;
        const text=clean(region.innerText);
        if(text.length>18000) break;
        const title=[...region.querySelectorAll('h1,h2,h3,div,p,span,strong,[role="heading"]')]
          .some(el=>visible(el)&&clean(el.innerText)===heading&&!el.contains(entry));
        if(title&&/岗位职责|工作职责|工作内容|任职要求|任职资格|职位描述|职位要求|岗位要求|job description|responsibilities|qualifications/i.test(text))
          return clean(entry.innerText);
      }
      return '';
    }""", navigation["heading"])


async def _portal_detail_entry(page: Page, navigation: dict) -> str:
    """Known public portal's detail-only delivery slot, never a form submit.

    Bare '投递' is intentionally NOT added to the global apply regex. It is
    allowed only inside this template's loaded JD, with a real visible title,
    one bounded CTA slot, no personal inputs and no form/auth dialog.
    """
    parsed = urlparse(page.url)
    if (not (parsed.hostname or '').casefold().endswith('.zhiye.com') or
            parsed.path.rstrip('/') not in {'/detail', '/campus/detail', '/social/detail', '/intern/detail'} or
            not navigation.get('heading') or not navigation.get('detail_content')):
        return ''
    return await page.evaluate(r"""heading => {
      const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
      const visible=el=>el&&el.getClientRects().length>0&&!el.disabled&&
        getComputedStyle(el).visibility!=='hidden'&&getComputedStyle(el).display!=='none';
      for(const el of document.querySelectorAll('[data-zhida-detail-entry]')) el.removeAttribute('data-zhida-detail-entry');
      const inputs=[...document.querySelectorAll('input,select,textarea')].filter(visible).filter(el=>
        !el.matches('[type="hidden"],[type="search"],[type="button"],[type="submit"]')&&
        !/搜索|关键词|关键字|search|keyword/i.test([el.placeholder,el.name,el.getAttribute('aria-label')].join(' ')));
      if(inputs.length) return '';
      const slots=[...document.querySelectorAll('[class*="STDeliverBtn"],[class~="editor__sc-ydltt0-30"]')].filter(slot=>{
        if(!visible(slot)||getComputedStyle(slot).pointerEvents==='none'||
           slot.closest('form,nav,header,footer,[role="dialog"],[aria-modal="true"],[aria-disabled="true"],[disabled],.disabled,[class*="recommend" i]')||
           slot.querySelector('input,select,textarea,[type="submit"],[aria-disabled="true"],[disabled],.disabled')) return false;
        if(!/^(投递|立即投递|投递简历|立即申请|申请职位|申请该职位|马上申请|去申请|apply now|apply for this job|start application)$/i.test(clean(slot.innerText))) return false;
        const region=slot.closest('[class~="editor__sc-ydltt0-9"],[class*="STComponent"]:not([class*="Container"]):not([class*="Item"])');
        if(!region||clean(region.innerText).length>18000) return false;
        const titles=[...region.querySelectorAll('[class*="STJobNameLeft"] > span,[class~="editor__sc-ydltt0-19"] > span')].filter(visible);
        return titles.length===1&&clean(titles[0].innerText)===heading&&
          /岗位职责|工作职责|工作内容|任职要求|任职资格|职位描述|职位要求|岗位要求|responsibilities|qualifications/i.test(clean(region.innerText));
      });
      if(slots.length!==1) return '';
      slots[0].setAttribute('data-zhida-detail-entry','portal');
      return clean(slots[0].innerText);
    }""", navigation['heading'])


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
    custom_entry = await _custom_detail_entry(page, navigation)
    portal_entry = await _portal_detail_entry(page, navigation)
    data = await page.evaluate(r"""
    () => {
      const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
      const visible = el => el.getClientRects().length > 0 && !el.disabled &&
        getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
      const buttons = [...document.querySelectorAll('button, a, [role="button"], input[type="submit"], input[type="button"]')]
        .filter(visible).map(el => clean(el.innerText || el.value || el.getAttribute('aria-label'))).filter(Boolean);
      // AutoHome renders div-based cards/CTAs. Only the uniquely expanded
      // verified detail can contribute an entry button; never all list CTAs.
      if(location.hostname==='talent.autohome.com.cn'&&location.pathname==='/campus-recruit-list.html'){
        const expanded=[...document.querySelectorAll('.position_card_li[pid]')].filter(card=>
          visible(card)&&[...card.children].some(el=>el.classList.contains('detail')&&visible(el)&&clean(el.innerText)));
        if(expanded.length===1){
          const card=expanded[0];
          for(const button of card.querySelectorAll('.overview .applybtn[pid]')){
            if(visible(button)&&button.getAttribute('pid')===card.getAttribute('pid')&&clean(button.innerText)==='申请该职位')
              buttons.push('申请该职位');
          }
        }
      }
      if(location.hostname==='talent.autohome.com.cn'&&location.pathname==='/recruit-delivery.html'){
        for(const button of document.querySelectorAll('.scard .save'))
          if(visible(button)&&clean(button.innerText)==='提交简历') buttons.push('提交简历');
      }
      const inputSelector = 'input:not([type="hidden"]):not([type="button"]):not([type="submit"]),select,textarea';
      const ownedQuestion = el => {
        const labelledBy = (el.getAttribute('aria-labelledby') || '').split(/\s+/)
          .map(id => document.getElementById(id)).filter(node => node && visible(node))
          .map(node => clean(node.innerText)).filter(Boolean).join(' ');
        if (labelledBy) return labelledBy;
        // Custom ATS labels are often sibling divs rather than <label for>.
        // Skip inner wrappers without a caption. Only inspect an owned row;
        // a section containing many
        // questions must not lend one question's title to another input.
        let row = el.parentElement;
        for (let depth = 0; row && depth < 12; depth++, row = row.parentElement) {
          if (row.matches('body,main,form,section,fieldset')) break;
          const fields = [...row.querySelectorAll(inputSelector)].filter(visible);
          if (fields.length !== 1 || fields[0] !== el) break;
          if (clean(row.innerText).length > 160) continue;
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
          placeholder: el.getAttribute('placeholder') || '',
          autocomplete: el.getAttribute('autocomplete') || '', checked: Boolean(el.checked)};
      });
      const bodyText = clean(document.body?.innerText).slice(0, 20000);
      const titleNode = document.querySelector('h1, .post_title, .post-name, .job-name, [class*="post-title" i], [class*="job-title" i], [class*="position-name" i]');
      const visibleTextNodes = [...document.querySelectorAll('h1,h2,h3,h4,h5,h6,legend,label,[role="heading"],span,p,div,dt')]
        .filter(node => visible(node) && !node.closest('nav,footer,[role="dialog"],[aria-modal="true"]'));
      const applicationTitleNodes = visibleTextNodes.map(node => ({node, value: clean(node.innerText)}))
        .filter(item => item.value.length <= 240)
        .map(item => ({...item, title: item.value.match(/^(?:您|你)?(?:正在|正)?(?:投递|申请)(?:的)?职位\s*[:：]\s*(.{2,200})$/)?.[1] || ''}))
        .filter(item => item.title && !/上传简历|个人信息|教育经历|加载中|暂无职位|未知职位/.test(item.title));
      // A parent wrapper may append application-count text to the exact title.
      // Keep innermost owned headings, not every containing div's innerText.
      const applicationTitles = applicationTitleNodes.filter(item => !applicationTitleNodes.some(other =>
        other.node !== item.node && item.node.contains(other.node))).map(item => item.title);
      const beisenAccount = location.hostname.endsWith('.zhiye.com') &&
        [...document.querySelectorAll('.print-nav span, .print-nav div')].some(node => visible(node) &&
          /^\+?86\s*\d{3}\*{3,}\d{4}$/.test(clean(node.innerText)));
      const shortLabels = [...new Set(visibleTextNodes.map(node => clean(node.innerText))
        .filter(value => value && value.length <= 40)
        .map(value => value.replace(/[\s*＊:：]/g, '')))];
      const formSections = shortLabels.filter(value => /^(个人信息|个人基本信息|基本信息|联系方式|教育经历|教育背景|工作经历|实习经历|学生实践经验|项目经历|语言能力|投递意向|求职意向|上传简历)$/.test(value));
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
      const accountRoot=document.querySelector('.new-login');
      const autohomeAccount=location.hostname==='account.autohome.com.cn'&&accountRoot&&visible(accountRoot)&&
        [...accountRoot.querySelectorAll('.tab-item[data-area]')].some(el=>visible(el)&&
          /^(微信登录|扫码登录|验证码登录|密码登录)$/.test(clean(el.innerText)));
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
        question_labels: questionLabels, auth_modal: authModal, storage_keys: storageKeys,
        autohome_account: Boolean(autohomeAccount), beisen_account: Boolean(beisenAccount)};
    }
    """)
    url = page.url
    site_route = await route_for_page(page)
    adapter = site_route.adapter
    parsed = urlparse(url)
    auth_route = bool(re.search(r"(?:^|/)(?:login|signin|sign-in|register|registration)(?:[/.]|$)", parsed.path, re.I))
    cookie_names = [item["name"] for item in await page.context.cookies([url]) if item.get("value")]
    text = data["body_text"]
    buttons: list[str] = data["buttons"]
    if custom_entry:
        buttons.append(custom_entry)
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
    if await inspect_autohome_account(page):
        authentication_evidence.append("汽车之家已登录账户菜单")
    if adapter == "beisen-italent" and data.get("beisen_account"):
        authentication_evidence.append("北森导航栏显示已登录的脱敏账户标识")
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
    autohome_account = bool(data.get("autohome_account"))
    if (active_auth_modal or autohome_account or
            (auth_route and (password or otp or login_button or registration_page))):
        authentication_evidence = []
    authenticated = bool(authentication_evidence)
    entry_confirmation = await has_entry_confirmation(page)
    search_fields = [item for item in inputs if item["type"] == "search" or re.search(
        r"搜索|关键词|关键字|search|keyword|搜索职位|搜索岗位", f"{item['label']} {item['name']} {item['placeholder']}", re.I)]
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

    apply_start_present = bool(portal_entry) or any(APPLY_START.match(button) for button in buttons)
    observed_job_id = navigation["job_id"]
    candidates = navigation["candidates"]
    beisen_detail_route = adapter == "beisen-italent" and (bool(re.search(
        r"(?:^|/)detail(?:/|$)", f"{parsed.path}/{parsed.fragment}", re.I)) or _custom_detail_route(url))
    application_route = bool(re.search(
        r"(?:^|/)(?:form|apply|application|resumeEdit)(?:/|$)", f"{parsed.path}/{urlparse(parsed.fragment).path}", re.I))
    if parsed.hostname == "talent.autohome.com.cn" and parsed.path == "/recruit-delivery.html":
        application_route = True
    # Moka's /apply/<tenant>/<site>#/jobs is the site root, not an
    # application form. For hash routers the fragment determines the stage.
    if adapter == "moka-campus" and re.search(r"^/?(?:jobs?(?:/|$)|page/)", urlparse(parsed.fragment).path):
        application_route = False
    # jobAdId persists into /form?fromPage=job&jobAdId=... . It identifies the
    # job throughout the flow; it does not mean every subsequent page is JD.
    detail_url = (beisen_detail_route or bool(observed_job_id)) and not (application_route or auth_route)
    explicit_job_heading = (bool(navigation["heading"]) and
        bool(navigation.get("detail_content")))
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
    elif autohome_account:
        stage, message = "auth_required", (
            "已进入汽车之家统一登录页，可在 Chrome 中选择微信扫码或验证码等方式。"
            "扫码成功仍需本人手机确认；完成登录并返回招聘官网后再重新识别。")
        methods = list(dict.fromkeys([*methods, "微信", "扫码", "手机验证码", "密码"]))
        actions = [WorkflowAction(intent="manual_login", label="在 Chrome 中完成汽车之家登录", requires_user=True),
                   WorkflowAction(intent="refresh", label="我已完成登录并返回招聘页", automated=True)]
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
    elif (auth_route and (registration_identifiers or unchecked_consent) and not form_evidence and
          (unchecked_consent or re.search(r"登录|注册|sign in|log in|login|register|create account", text, re.I)) and not authenticated):
        # Passwordless ATS login commonly asks for an identifier first, with
        # a div-based 'Next' control and an OTP field only on the next screen.
        # Some sites hide all login controls until privacy is accepted. A
        # visible privacy-consent checkbox on the auth route is also a handoff.
        # Require the auth route + real identifier/auth wording OR consent;
        # neither a Login header on a job page nor a URL alone is authority.
        stage, message = 'auth_required', (
            '已进入招聘网站的登录/隐私确认页。请在招聘浏览器亲自核对隐私协议，'
            '输入手机号或邮箱并完成验证码；完成后回来同步当前页面。系统不会代替你同意协议。')
        actions = [WorkflowAction(intent='manual_login', label='在招聘浏览器完成登录', requires_user=True),
                   WorkflowAction(intent='refresh', label='我已完成登录', automated=True)]
    elif (password or active_auth_modal or (auth_route and login_button) or (login_button and not detail_url and not application_route and
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
    elif form_evidence and form_fields and not (job_list_evidence or home_evidence):
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
        message=message, job_title=(data.get("application_job_title") or navigation["heading"]), job_id=job_id,
        page_evidence=(navigation["text"][:8000] if stage in {"homepage", "job_list", "job_detail"} else ""),
        authenticated=authenticated, authentication_evidence=list(dict.fromkeys(authentication_evidence)),
        entry_confirmation_required=entry_confirmation,
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


async def start_application(page: Page, target: ApplicationTarget | None = None) -> None:
    target = target or ApplicationTarget()
    state = await inspect_application_page(page, "check", target)
    if state.stage != "job_detail":
        raise ValueError("只能从已识别的职位详情页进入申请流程")
    parsed = urlparse(page.url)
    if parsed.hostname == "talent.autohome.com.cn" and parsed.path == "/campus-recruit-list.html":
        if not re.fullmatch(r"\d+", state.job_id):
            raise ValueError("汽车之家当前岗位标识不明确，请先展开唯一的职位详情")
        await enter_selected_application(page, state.job_id, state.job_title, target)
        return
    custom_entry = await _custom_detail_entry(page, await observe_navigation(page, target))
    portal_entry = await _portal_detail_entry(page, await observe_navigation(page, target))
    if portal_entry:
        entry = page.locator('[data-zhida-detail-entry="portal"]')
        if await entry.count() != 1 or not await entry.is_visible():
            raise ValueError('岗位详情申请入口已变化，请重新识别')
        await entry.click(timeout=4000)
        await page.wait_for_timeout(1500)
        return
    candidates = page.locator("button, a, [role=button]" + (", #apply" if custom_entry else ""))
    eligible = []
    for index in range(await candidates.count()):
        item = candidates.nth(index)
        if not await item.is_visible() or await item.is_disabled():
            continue
        if await item.evaluate("el => Boolean(el.closest('form,[role=dialog],[aria-modal=true]')) || el.matches('[type=submit]') || el.getAttribute('aria-disabled') === 'true'"):
            continue
        text = re.sub(r"\s+", " ", (await item.inner_text()).strip())
        if APPLY_START.fullmatch(text) and not FINAL_SUBMIT.fullmatch(text):
            eligible.append(item)
    if len(eligible) != 1:
        raise ValueError("未找到唯一可安全进入申请的入口，请重新识别或由用户选择，系统不会猜测或点击提交")
    click_errors: list[str] = []
    for item in eligible:
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
