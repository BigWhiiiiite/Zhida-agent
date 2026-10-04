"""Observed-page-only job navigation. No invented URLs, arbitrary clicks or submits."""
from __future__ import annotations

import hashlib
import ipaddress
import re
import unicodedata
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import Page

from .browser_models import ApplicationTarget, NavigationCandidate


LIST_LABEL = re.compile(r"^(校园招聘|校招职位|招聘职位|招聘岗位|职位列表|岗位列表|查看职位|查看岗位|全部职位|全部岗位|投递简历|加入我们|campus jobs|careers|jobs|view jobs|open positions)$", re.I)
SEARCH_HINT = re.compile(r"职位|岗位|关键字|关键词|job|position|keyword", re.I)
NON_JOB_SEARCH = re.compile(r"地点|城市|地区|location|city|country", re.I)
SEARCH_LABEL = re.compile(r"^(搜索|查询|搜索职位|搜索岗位|search|search jobs|find jobs)$", re.I)


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def _autohome_campus_list(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.hostname or "").casefold() == "talent.autohome.com.cn" and parsed.path == "/campus-recruit-list.html"


def _beisen_job_list(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.hostname or "").casefold().endswith(".zhiye.com") and parsed.path.rstrip("/") in {
        "/jobs", "/campus/jobs", "/social/jobs", "/intern/jobs"
    }


def beisen_search_keyword(target: ApplicationTarget) -> str:
    """Search by the role, not a decorated title copied from a poster.

    Only remove known cohort/city/job-number decorations. The exact requested
    title remains the authority when selecting the result, never this keyword.
    """
    value = unicodedata.normalize("NFKC", target.job_title).strip()
    value = re.sub(r"\s*\(J\d+\)\s*$", "", value, flags=re.I)
    value = re.sub(r"[-_]\d{3,8}\s*$", "", value)
    if target.city:
        city = unicodedata.normalize("NFKC", target.city).removesuffix("市")
        value = re.sub(r"\(" + re.escape(city) + r"市?\)", "", value)
    value = re.sub(r"^(?:20)?\d{2}(?:届)?(?:秋招|春招|校招)[\s\-_—]*", "", value)
    return value.strip() or target.job_title.strip()


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
    keys = {"postid", "jobid", "positionid", "jobadid"}
    # pid is a generic product/page key elsewhere. Only the observed Autohome
    # campus routes establish that it denotes a recruitment position.
    if (parsed.hostname or "").casefold() == "talent.autohome.com.cn" and parsed.path in {
        "/campus-recruit-list.html", "/recruit-delivery.html"
    }:
        keys.add("pid")
    for raw_query in (parsed.query, urlparse(parsed.fragment).query):
        for key, values in parse_qs(raw_query).items():
            if key.casefold().replace("_", "") not in keys:
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


def _target_matches(label: str, context: str, page_text: str, target: ApplicationTarget, page_url: str = "") -> bool:
    wanted = _norm(target.job_title)
    if len(wanted) < 3 or wanted not in _norm(label):
        return False
    # City belongs to the particular card, never another result on the page.
    if target.city and _norm(target.city).removesuffix("市") not in _norm(context):
        return False
    if target.company and _norm(target.company) not in _norm(page_text):
        # A site-grounded alias, not a generic fuzzy company-name match.
        official_360 = (urlparse(page_url).hostname or "").casefold() == "360campus.zhiye.com"
        if not (official_360 and _norm(target.company) in {"奇虎360", "360", "360集团"}
                and "360集团" in page_text):
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
    # ATS titles often say '27秋招'. Require an explicit cohort suffix; a
    # salary, quantity or publication date cannot establish a graduate year.
    page_years.update("20" + year for year in re.findall(
        r"(?<!\d)(\d{2})(?=届(?:校招|毕业生)|届?秋招|届?春招|届?校招)", page_text))
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
    autohome_list = _autohome_campus_list(page.url)
    beisen_list = _beisen_job_list(page.url)
    parsed = urlparse(page.url)
    beisen_detail = ((parsed.hostname or '').casefold().endswith('.zhiye.com') and
        parsed.path.rstrip('/') in {'/detail', '/campus/detail', '/social/detail', '/intern/detail'} and
        bool(job_identity(page.url)))
    observed = await page.evaluate(r"""({autohomeList,beisenList,beisenDetail}) => {
      const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
      const visible=el=>el.getClientRects().length>0&&!el.disabled&&
        getComputedStyle(el).visibility!=='hidden'&&getComputedStyle(el).display!=='none';
      const meaningfulHeading=el=>{
        const value=clean(el?.innerText);
        return el&&visible(el)&&value.length>=2&&value.length<=200&&
          !/^(职位详情|岗位详情|招聘职位|招聘岗位|校园招聘|社会招聘|人才招聘|招聘官网|职位列表|岗位列表|job details?|careers|open positions)$/i.test(value)&&
          !el.closest('nav,header,footer,a,button,[role="button"],[role="dialog"],[aria-modal="true"],[class*="job-card"],[class*="job-item"],[class*="position-item"],[class*="recommend" i]');
      };
      // Actual visible titles only. Camel-case Beisen title classes are not
      // matched by the old job-title selector. Never derive a title from the
      // requested target, URL, document.title, recommendation card or hidden DOM.
      const titleClass=el=>[...el.classList].some(name=>
        /^(?:posttitle|postname|jobtitle|jobname|positiontitle|positionname)$/i.test(name.replace(/[-_]/g,''))||
        /^(?:job[-_]?name|job[-_]?title|position[-_]?title)[_-][a-z0-9_-]+$/i.test(name));
      const explicitTitles=[...document.querySelectorAll(
        '[class],[data-testid="job-title"],[data-test="job-title"]')].filter(el=>
          titleClass(el)||el.matches('[data-testid="job-title"],[data-test="job-title"]'));
      const detailTitles=[...document.querySelectorAll(
        '[class*="job-detail" i] .detail-title,[class*="jobDetail"] .detail-title,'+
        '[class*="position-detail" i] .detail-title,[class*="positionDetail"] .detail-title')];
      let heading=[...explicitTitles,...detailTitles,...document.querySelectorAll('h1,[class*="job-detail" i] h2,[class*="jobDetail"] h2,[class*="job-name" i],[class*="jobName"]')].find(meaningfulHeading);
      const jdLabel=/岗位职责|工作职责|工作内容|任职要求|任职资格|职位描述|职位要求|岗位要求|job description|responsibilities|qualifications/i;
      const captionOnly=/^(岗位职责|工作职责|工作内容|任职要求|任职资格|职位描述|职位要求|岗位要求|job description|responsibilities|qualifications)\s*[:：]?$/i;
      // Do not mistake loading headings for loaded JD content. At least one
      // visible description body must exist after a responsibility caption.
      const descriptionIn=root=>{
        const nodes=[...root.querySelectorAll('h1,h2,h3,h4,h5,h6,div,p,span,dt,dd,li,section')]
          .filter(el=>visible(el)&&!el.closest('nav,header,footer,a,button,[role="button"],[role="dialog"],[class*="recommend" i],[class*="job-card"],[class*="job-item"],[class*="position-item"]'));
        return nodes.some(el=>{
          const value=clean(el.innerText);
          if(!jdLabel.test(value)||value.length>14000) return false;
          const inline=value.replace(jdLabel,'').replace(/^[\s:：]+/,'');
          if(!captionOnly.test(value)&&inline.length>=4&&!/^(加载中|正在加载|loading)[.。…\s]*$/i.test(inline)&&
             ![...el.querySelectorAll('h1,h2,h3,h4,h5,h6,div,p,span,dt')].some(child=>captionOnly.test(clean(child.innerText)))&&
             !el.querySelector('input,select,textarea,button,a,[role="button"]')) return true;
          if(!captionOnly.test(value)) return false;
          const sibling=el.nextElementSibling;
          const body=clean(sibling?.innerText);
          return sibling&&visible(sibling)&&body.length>=4&&body.length<=12000&&!captionOnly.test(body)&&
            !/^(加载中|正在加载|loading)[.。…\s]*$/i.test(body)&&
            !sibling.querySelector('input,select,textarea,button,a,[role="button"]')&&
            !sibling.matches('button,a,[role="button"],input,select,textarea');
        });
      };
      // Public Beisen detail template: the title is STJobNameLeft > span,
      // not h1/jobTitle. Bind it to its own STComponent and loaded JD body;
      // neither document.title nor a recommendation/list card establishes it.
      if(!heading&&beisenDetail){
        const titles=[...document.querySelectorAll('[class*="STJobNameLeft"] > span,[class~="editor__sc-ydltt0-19"] > span')]
          .filter(el=>{
            const region=el.closest('[class~="editor__sc-ydltt0-9"],[class*="STComponent"]:not([class*="Container"]):not([class*="Item"])');
            return meaningfulHeading(el)&&!el.querySelector('a,button,input,select,textarea,[role="button"]')&&
              region&&descriptionIn(region);
          });
        if(titles.length===1) heading=titles[0];
      }
      // White-label ATS templates frequently render `.title`/`.detail-title`
      // or a plain paragraph instead of h1/jobTitle. Require an actual visible
      // short node in a bounded JD region. document.title only corroborates
      // that node; it can never manufacture a title from a URL/target/shell.
      if(!heading){
        const norm=v=>clean(v).replace(/[\s·|｜—_\-]/g,'').toLowerCase();
        const titleText=norm(document.title);
        const candidates=[...document.querySelectorAll('h2,h3,[role="heading"],div,p,span,strong')].filter(el=>{
          if(!meaningfulHeading(el)||el.querySelector('button,a,input,select,textarea,[role="button"]')) return false;
          const value=clean(el.innerText);
          if(captionOnly.test(value)||/^(工作地点|职位类别|招聘人数|发布时间|截止时间|加载中|loading)\s*[:：]?/i.test(value)) return false;
          // Prefer the innermost title, not a surrounding description block.
          if([...el.children].some(child=>visible(child)&&clean(child.innerText)===value)) return false;
          const semantic=el.matches('h2,h3,[role="heading"]')||[...el.classList].some(name=>
            /^(?:(?:job|position|post|recruit|detail)[-_]?)?(?:title|name)(?:[-_][a-z0-9]+)?$/i.test(name));
          const corroborated=norm(value)===titleText;
          if(!semantic&&!corroborated) return false;
          let region=el.parentElement;
          for(let depth=0;region&&depth<5;depth++,region=region.parentElement){
            if(region.matches('body,html,nav,header,footer,form')) break;
            if(clean(region.innerText).length<=18000&&descriptionIn(region)) return true;
          }
          return false;
        });
        const exact=candidates.filter(el=>norm(el.innerText)===titleText);
        const pool=exact.length?exact:candidates;
        const unique=[...new Set(pool.map(el=>clean(el.innerText)))];
        if(unique.length===1) heading=pool[0];
      }
      const detailContent=descriptionIn(document.body);
      const nodes=[...document.querySelectorAll('a[href], button, [role="button"], input[type="search"], input[placeholder], input[aria-label]')].filter(visible);
      // The official campus script delegates positionClick to these div cards.
      // Mark the title, never the card centre, which can hit .applybtn instead.
      // No destination URL is invented for an in-place detail expansion.
      const autohomeCards=autohomeList?[...document.querySelectorAll('.pcards > .position_card_li[pid]')].filter(card=>{
        const title=card.querySelector(':scope > .overview > .left > .pname');
        return visible(card)&&/^[0-9]{1,20}$/.test(card.getAttribute('pid')||'')&&
          title&&visible(title)&&clean(title.innerText).length>=2&&clean(title.innerText).length<=200&&
          !title.closest('a,button,[role="button"],.applybtn')&&
          !title.querySelector('a,button,input,[role="button"],.applybtn')&&
          card.querySelector(':scope > .detail');
      }):[];
      const expandedCards=autohomeCards.filter(card=>{
        const detail=card.querySelector(':scope > .detail');
        return visible(detail)&&/工作职责|岗位要求/.test(clean(detail.innerText))&&
          [...detail.querySelectorAll('.dutyCt,.jobCt')].some(el=>visible(el)&&clean(el.innerText));
      });
      const activeCard=expandedCards.length===1?expandedCards[0]:null;
      if(autohomeList) heading=activeCard?.querySelector(':scope > .overview > .left > .pname');
      const expandableTitles=new Map();
      for(const card of autohomeCards){
        // A repeated open action must never toggle a visible detail closed.
        if(visible(card.querySelector(':scope > .detail'))) continue;
        const title=card.querySelector(':scope > .overview > .left > .pname');
        expandableTitles.set(title,card);
        if(!nodes.includes(title)) nodes.push(title);
      }
      // Beisen's public portal list component attaches detail navigation to
      // STJobTitle's direct span. The card body only expands a preview, and
      // its footer contains application CTAs. Never click either of those.
      // Support semantic displayName classes and the observed component IDs;
      // this is a template adapter, not an arbitrary div-click heuristic.
      const beisenTitles=new Map();
      if(beisenList){
        const titleSelector='[class*="STJobTitle"],[class~="editor__sc-10r1nhd-4"]';
        const cardSelector='[class*="STListItem"]:not([class*="Content"]),[class~="editor__sc-10r1nhd-0"]';
        for(const titleBox of document.querySelectorAll(titleSelector)){
          const card=titleBox.closest(cardSelector);
          const titles=[...titleBox.children].filter(el=>el.tagName==='SPAN'&&visible(el));
          if(!card||!visible(card)||titles.length!==1||
             card.querySelectorAll(titleSelector).length!==1) continue;
          const title=titles[0], label=clean(title.innerText), context=clean(card.innerText);
          if(label.length<2||label.length>200||context.length>14000||
             title.closest('a,button,[role="button"]')||
             title.querySelector('a,button,input,select,textarea,[role="button"]')) continue;
          // Stable, observed provenance; no fabricated jobAdId or URL.
          const key=JSON.stringify([label,context]);
          beisenTitles.set(title,{card,key});
          if(!nodes.includes(title)) nodes.push(title);
        }
      }
      const keyCounts=new Map(),keyOccurrences=new Map();
      for(const {key} of beisenTitles.values()) keyCounts.set(key,(keyCounts.get(key)||0)+1);
      // Removed/hidden candidates (including a now-expanded title) must not
      // retain a marker that is reassigned to another visible control.
      for(const el of document.querySelectorAll('[data-zhida-nav]')) el.removeAttribute('data-zhida-nav');
      const items=nodes.slice(0,400).map((el,index)=>{
        const marker=`n${index}`;el.setAttribute('data-zhida-nav',marker);
        const cardTitle=el.querySelector('[class*="job-title" i],[class*="jobTitle"],[class*="job-name" i],h2,h3');
        const label=clean(cardTitle?.innerText||el.innerText||el.getAttribute('aria-label')||el.getAttribute('placeholder')||el.value);
        const expansionCard=expandableTitles.get(el);
        const beisenTitle=beisenTitles.get(el);
        const card=expansionCard||beisenTitle?.card||el.closest('[class*="job-item"], [class*="job-card"], [class*="position-item"], li, article');
        let observedKey=beisenTitle?.key||'';
        if(beisenTitle&&keyCounts.get(observedKey)>1){
          const occurrence=keyOccurrences.get(observedKey)||0;
          keyOccurrences.set(observedKey,occurrence+1);
          observedKey+=`#duplicate=${occurrence}`;
        }
        // Public portal's navigation search is Enter-driven (not onChange).
        // Only this exact, non-form widget on a recognized list route gets
        // that behaviour. Never press Enter in a real application form.
        const beisenSearch=beisenList&&el.tagName==='INPUT'&&!el.closest('form')&&
          /^(搜索职位关键词|搜索職位關鍵詞|Search for job keywords)$/.test(clean(el.getAttribute('placeholder')));
        return {marker,label,href:el.tagName==='A'?el.getAttribute('href')||'':'',
          input:el.tagName==='INPUT',type:el.type||'',context:clean(card?.innerText||label).slice(0,1000),
          expansionPid:expansionCard?.getAttribute('pid')||'',
          observedKey,beisenTitle:Boolean(beisenTitle),beisenSearch,
          locationContext:beisenTitle?clean((card.querySelector('[class*="STLabelSection"],[class~="editor__sc-10r1nhd-11"],[class*="STOtherSection"]'))?.innerText):'',
          duplicateCount:beisenTitle?keyCounts.get(beisenTitle.key):0,
          searchForm:Boolean(el.closest('form')?.querySelector('button[type="submit"],input[type="submit"]'))};
      });
      return {items,title:document.title,text:clean(document.body?.innerText).slice(0,24000),
        heading:clean(heading?.innerText),detailContent,expandedJobId:activeCard?.getAttribute('pid')||''};
    }""", {"autohomeList": autohome_list,"beisenList":beisen_list,"beisenDetail":beisen_detail})
    candidates = []
    private = {}
    seen = set()
    for item in observed["items"]:
        label = item["label"]
        if not label or len(label) > 400:
            continue
        href = safe_observed_url(item["href"], page.url) if item["href"] else ""
        if item["input"]:
            if item["type"] not in {"text", "search"} or not SEARCH_HINT.search(label) or NON_JOB_SEARCH.search(label):
                continue
            kind = "search_jobs"
        elif item["type"] in {"submit", "reset"}:
            continue
        elif item["expansionPid"] or item["beisenTitle"]:
            kind = "open_job"
        elif (href and job_identity(href) and not LIST_LABEL.fullmatch(label) and not (
                (urlparse(href).hostname or "").casefold() == "talent.autohome.com.cn" and
                urlparse(href).path == "/recruit-delivery.html")):
            kind = "open_job"
        elif (LIST_LABEL.fullmatch(label) or (href and re.search(r"(?:#/jobs(?:\?|$)|/jobs/?$|/positions/?$)", href))):
            kind = "browse_jobs"
        else:
            continue
        if re.search(r"投递|提交|申请|\b(?:apply|submit)\b", label, re.I) and not (
                kind == "browse_jobs" and href and
                re.search(r"(?:#/page/|#/jobs(?:\?|$)|/jobs/?$|/positions/?$)", href)):
            # An ambiguous CTA may be final submission, especially on a
            # read-only preview. Only an observed listing/section link qualifies.
            continue
        if item["href"] and not href:
            continue
        unique = (kind, href, label, item["expansionPid"], item["observedKey"])
        if unique in seen:
            continue
        seen.add(unique)
        identity = f"{page.url}|{kind}|{href}|{label}"
        if item["expansionPid"]:
            identity += f'|pid={item["expansionPid"]}'
        if item["observedKey"]:
            identity += f'|card={item["observedKey"]}'
        identifier = hashlib.sha256(identity.encode()).hexdigest()[:24]
        city_context = item["locationContext"] if item["beisenTitle"] else item["context"]
        matches_target = kind == "open_job" and _target_matches(label,city_context,
            observed["title"] + " " + observed["text"],target,page.url)
        if item["expansionPid"] or item["beisenTitle"]:
            # A neighbouring card's cohort is not evidence for this result.
            matches_target = matches_target and recruitment_cycle_matches(target.recruitment_cycle,item["context"])
            source_id = job_identity(target.source_url) if _autohome_campus_list(target.source_url) else ""
            if source_id:
                matches_target = matches_target and source_id == item["expansionPid"]
        candidate = NavigationCandidate(id=identifier,label=label,url=href,kind=kind,
            matches_target=matches_target)
        candidates.append(candidate)
        private[identifier] = item
    return {"candidates":candidates,"private":private,"text":observed["text"],
            "heading":observed["heading"],
            "detail_content":observed["detailContent"],
            "job_id":observed["expandedJobId"] if autohome_list else job_identity(page.url)}


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
    if kind == "browse_jobs" and re.search(r"校招|校园|campus|毕业|20\d{2}",
                                            target.recruitment_cycle + " " + target.source_url, re.I):
        campus = [item for item in allowed if re.search(r"校招|校园|campus", item.label + " " + item.url, re.I)]
        # A single generic 'Join us' link is not proof of a campus entrance.
        allowed = campus or [item for item in allowed
                             if not re.search(r"社招|社会招聘|social-recruitment|/apply/", item.label + " " + item.url, re.I)
                             and not re.fullmatch(r"加入我们|careers", item.label, re.I)]
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
    if item["duplicateCount"] > 1:
        raise ValueError("多个岗位卡片的标题和内容完全相同，无法安全区分；请在招聘网页中明确选择")
    if kind == "search_jobs":
        if not target.job_title.strip():
            raise ValueError("请先提供目标岗位名称")
        await control.fill(beisen_search_keyword(target) if item["beisenSearch"] else target.job_title,timeout=5000)
        if item["beisenSearch"]:
            await control.press("Enter",timeout=5000)
            await page.wait_for_timeout(700)
            return candidate
        form = control.locator('xpath=ancestor::form[1]')
        roots = [form] if await form.count() else [page]
        buttons = roots[0].locator('button, [role="button"], input[type="submit"], input[type="button"]')
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
                        str(getattr(workflow, 'entry_confirmation_required', False)),
                        str(workflow.form_fields), *(f'{item.kind}:{item.url}:{item.label}' for item in workflow.navigation_candidates)))
    return hashlib.sha256(payload.encode()).hexdigest()
