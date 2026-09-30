"""Observed-page-only job navigation. No invented URLs, arbitrary clicks or submits."""
from __future__ import annotations

import hashlib
import ipaddress
import re
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import Page

from .browser_models import ApplicationTarget, NavigationCandidate


LIST_LABEL = re.compile(r"^(校园招聘|校招职位|招聘职位|招聘岗位|职位列表|岗位列表|查看职位|查看岗位|全部职位|全部岗位|投递简历|加入我们|campus jobs|careers|jobs|view jobs|open positions)$", re.I)
SEARCH_HINT = re.compile(r"职位|岗位|关键字|关键词|job|position|keyword", re.I)
SEARCH_LABEL = re.compile(r"^(搜索|查询|搜索职位|搜索岗位|search|search jobs|find jobs)$", re.I)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def job_identity(url: str) -> str:
    """Read a locator from an observed URL, not proof that the job loaded.

    Beisen uses jobAdId (including mixed-case query names in shared links).
    Hash-router query parameters are supported, but ambiguous identifiers are
    deliberately not collapsed into an arbitrary first job.
    """
    parsed = urlparse(url)
    route = f"{parsed.path}/{parsed.fragment}"
    match = re.search(r"(?:^|/)(?:job|jobs|position|positions)/(?!list(?:[/?#]|$))([^/?#]+)", route, re.I)
    if match:
        return match.group(1)
    identifiers = set()
    for raw_query in (parsed.query, urlparse(parsed.fragment).query):
        for key, values in parse_qs(raw_query).items():
            if key.casefold().replace("_", "") not in {"postid", "jobid", "positionid", "jobadid"}:
                continue
            for value in values:
                value = value.strip()
                if value and len(value) <= 200 and not re.search(r"[\s/?#\\\x00-\x1f]", value):
                    identifiers.add(value)
    return next(iter(identifiers)) if len(identifiers) == 1 else ""


def safe_observed_url(value: str, base: str) -> str:
    url = urljoin(base, value)
    parsed = urlparse(url)
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    host = parsed.hostname.casefold()
    if host == "localhost" or host.endswith((".local", ".internal")):
        return ""
    try:
        if not ipaddress.ip_address(host).is_global:
            return ""
    except ValueError:
        pass
    return url


def _target_matches(label: str, context: str, page_text: str, target: ApplicationTarget) -> bool:
    wanted = _norm(target.job_title)
    if len(wanted) < 3 or wanted not in _norm(label):
        return False
    # City belongs to the particular card, never another result on the page.
    if target.city and _norm(target.city).removesuffix("市") not in _norm(context):
        return False
    if target.company and _norm(target.company) not in _norm(page_text):
        return False
    if not recruitment_cycle_matches(target.recruitment_cycle, page_text):
        return False
    return True


def recruitment_cycle_matches(value: str, page_text: str) -> bool:
    """Normalize cohort wording, without silently changing year or season."""
    if not value.strip():
        return True
    years = set(re.findall(r"(?<!\d)20\d{2}(?!\d)", value))
    if not years:
        return _norm(value) in _norm(page_text)
    page_years = set(re.findall(r"(?<!\d)20\d{2}(?!\d)", page_text))
    if not years.issubset(page_years):
        return False
    # 2027校招, 2027届 and 2027 Campus Recruitment share a cohort;
    # an explicitly requested autumn/spring batch still needs evidence.
    for season in ("春", "秋"):
        if season in value and season not in page_text:
            return False
    return True


async def observe_navigation(page: Page, target: ApplicationTarget | None = None) -> dict:
    target = target or ApplicationTarget()
    observed = await page.evaluate(r"""() => {
      const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
      const visible=el=>el.getClientRects().length>0&&!el.disabled&&
        getComputedStyle(el).visibility!=='hidden'&&getComputedStyle(el).display!=='none';
      const meaningfulHeading=el=>{
        const value=clean(el?.innerText);
        return el&&visible(el)&&value.length>=2&&value.length<=200&&
          !/^(职位详情|岗位详情|招聘职位|招聘岗位|校园招聘|社会招聘|人才招聘|招聘官网|职位列表|岗位列表|job details?|careers|open positions)$/i.test(value)&&
          !el.closest('nav,header,footer,[class*="job-card"],[class*="job-item"],[class*="position-item"]');
      };
      // Actual visible titles only. Camel-case Beisen title classes are not
      // matched by the old job-title selector. Never derive a title from the
      // requested target, URL, document.title, recommendation card or hidden DOM.
      const titleClass=el=>[...el.classList].some(name=>
        /^(?:posttitle|postname|jobtitle|jobname|positiontitle|positionname)$/i.test(name.replace(/[-_]/g,'')));
      const explicitTitles=[...document.querySelectorAll(
        '[class],[data-testid="job-title"],[data-test="job-title"]')].filter(el=>
          titleClass(el)||el.matches('[data-testid="job-title"],[data-test="job-title"]'));
      const detailTitles=[...document.querySelectorAll(
        '[class*="job-detail" i] .detail-title,[class*="jobDetail"] .detail-title,'+
        '[class*="position-detail" i] .detail-title,[class*="positionDetail"] .detail-title')];
      const heading=[...explicitTitles,...detailTitles,...document.querySelectorAll('h1')].find(meaningfulHeading);
      const nodes=[...document.querySelectorAll('a[href], button, [role="button"], input[type="search"], input[placeholder], input[aria-label]')].filter(visible);
      const items=nodes.slice(0,400).map((el,index)=>{
        const marker=`n${index}`;el.setAttribute('data-zhida-nav',marker);
        const label=clean(el.innerText||el.getAttribute('aria-label')||el.getAttribute('placeholder')||el.value);
        const card=el.closest('[class*="job-item"], [class*="job-card"], [class*="position-item"], li, article');
        return {marker,label,href:el.tagName==='A'?el.getAttribute('href')||'':'',
          input:el.tagName==='INPUT',type:el.type||'',context:clean(card?.innerText||label).slice(0,1000),
          searchForm:Boolean(el.closest('form')?.querySelector('button[type="submit"],input[type="submit"]'))};
      });
      return {items,title:document.title,text:clean(document.body?.innerText).slice(0,24000),
        heading:clean(heading?.innerText)};
    }""")
    candidates = []
    private = {}
    seen = set()
    for item in observed["items"]:
        label = item["label"]
        if not label or len(label) > 400:
            continue
        href = safe_observed_url(item["href"], page.url) if item["href"] else ""
        if item["input"]:
            if item["type"] not in {"text", "search"} or not SEARCH_HINT.search(label):
                continue
            kind = "search_jobs"
        elif item["type"] in {"submit", "reset"}:
            continue
        elif href and job_identity(href) and not LIST_LABEL.fullmatch(label):
            kind = "open_job"
        elif (LIST_LABEL.fullmatch(label) or (href and re.search(r"(?:#/jobs(?:\?|$)|/jobs/?$|/positions/?$)", href))):
            kind = "browse_jobs"
        else:
            continue
        if re.search(r"投递|提交|申请", label) and not (
                kind == "browse_jobs" and href and
                re.search(r"(?:#/page/|#/jobs(?:\?|$)|/jobs/?$|/positions/?$)", href)):
            # An ambiguous CTA may be final submission, especially on a
            # read-only preview. Only an observed listing/section link qualifies.
            continue
        if item["href"] and not href:
            continue
        unique = (kind, href, label)
        if unique in seen:
            continue
        seen.add(unique)
        identifier = hashlib.sha256(f"{page.url}|{kind}|{href}|{label}".encode()).hexdigest()[:24]
        candidate = NavigationCandidate(id=identifier,label=label,url=href,kind=kind,
            matches_target=kind == "open_job" and _target_matches(label,item["context"],
                observed["title"] + " " + observed["text"],target))
        candidates.append(candidate)
        private[identifier] = item
    return {"candidates":candidates,"private":private,"text":observed["text"],
            "heading":observed["heading"],"job_id":job_identity(page.url)}


def choose_candidate(candidates: list[NavigationCandidate], kind: str, target: ApplicationTarget,
                     candidate_id: str = "") -> NavigationCandidate:
    allowed = [item for item in candidates if item.kind == kind]
    if candidate_id:
        chosen = next((item for item in allowed if item.id == candidate_id), None)
        if not chosen:
            raise ValueError("页面候选已经变化，请重新识别后选择真实入口")
        return chosen
    if kind == "open_job":
        allowed = [item for item in allowed if item.matches_target] if target.job_title.strip() else []
    if kind == "browse_jobs" and len(allowed) > 1:
        explicit_lists = [item for item in allowed if re.search(r"职位|岗位|jobs|positions", item.label, re.I)
                          or re.search(r"(?:#/jobs|/jobs/?$|/positions/?$)", item.url)]
        if len(explicit_lists) == 1:
            allowed = explicit_lists
    if kind == "search_jobs" and not target.job_title.strip():
        raise ValueError("请先填写目标岗位名称，搜索工具不会猜测目标")
    if len(allowed) != 1:
        raise ValueError("没有唯一可靠的导航目标，请从当前网页候选中明确选择")
    return allowed[0]


async def execute_navigation(page: Page, kind: str, target: ApplicationTarget,
                             candidate_id: str = "") -> NavigationCandidate:
    observed = await observe_navigation(page,target)
    candidate = choose_candidate(observed["candidates"],kind,target,candidate_id)
    item = observed["private"][candidate.id]
    control = page.locator(f'[data-zhida-nav="{item["marker"]}"]')
    if await control.count() != 1 or not await control.is_visible():
        raise ValueError("导航控件已变化，请重新识别")
    if kind == "search_jobs":
        if not target.job_title.strip():
            raise ValueError("请先提供目标岗位名称")
        await control.fill(target.job_title,timeout=5000)
        form = control.locator('xpath=ancestor::form[1]')
        roots = [form] if await form.count() else [page]
        buttons = roots[0].locator('button, [role="button"], input[type="submit"]')
        matches = []
        for index in range(await buttons.count()):
            button=buttons.nth(index)
            if await button.is_visible() and SEARCH_LABEL.fullmatch((await button.inner_text() or await button.get_attribute('value') or '').strip()):
                matches.append(button)
        if len(matches) == 1:
            await matches[0].click(timeout=5000)
        # Reactive search may update on input. Never press Enter in a generic
        # form because it could submit a real application.
    else:
        await control.click(timeout=8000)
    await page.wait_for_timeout(700)
    return candidate


def workflow_fingerprint(workflow) -> str:
    payload = "|".join((workflow.url, workflow.stage, workflow.job_id, workflow.job_title,
                        str(workflow.form_fields), *(f'{item.kind}:{item.url}:{item.label}' for item in workflow.navigation_candidates)))
    return hashlib.sha256(payload.encode()).hexdigest()
