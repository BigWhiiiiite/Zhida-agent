"""Autohome public DOM regression in intercepted, anonymous Chromium only.

Structure and delegated click behaviour observed in the official public script:
https://talent.autohome.com.cn/js/school-recruit-list.js?v=20251118
No application, delivery, account or real network request is made by this test.
"""
from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright

from app.browser_models import ApplicationTarget
from app.job_navigation import choose_candidate, execute_navigation, job_identity, observe_navigation


BASE = "https://talent.autohome.com.cn/campus-recruit-list.html?targetId=1004"
TITLE = "AI开发工程师【2027届校招】"
TARGET = ApplicationTarget(company="汽车之家", job_title="AI开发工程师", city="北京", recruitment_cycle="2027校招")
DETAIL = '<div class="duty">工作职责</div><div class="dutyCt"><p>开发大模型应用。</p></div><div class="job">岗位要求</div><div class="jobCt"><p>掌握 Python。</p></div>'


def card(pid: str, title: str = TITLE, city: str = "北京", *, expanded: bool = False, hidden: bool = False) -> str:
    return f'''<div class="position_card_li" pid="{pid}" {'style="display:none"' if hidden else ''}>
      <div class="overview"><div class="left"><div class="pname">{title}</div><div class="pother"><div>{city}</div></div></div>
        <div class="right"><div class="btn applybtn" pid="{pid}">申请该职位</div>
          <div class="openbtn"></div><div class="closebtn" style="display:none"></div></div></div>
      <div class="detail" style="display:{'block' if expanded else 'none'}">{DETAIL if expanded else ''}</div></div>'''


def fixture(cards: str) -> str:
    return '''<!doctype html><html><title>汽车之家校园招聘</title><body>
      <style>.overview{display:flex}.left{width:320px}.right{width:200px}.pname,.applybtn{padding:15px}</style>
      <header>汽车之家 <a href="campus-recruit-list.html?targetId=1004">校园招聘</a></header>
      <div class="position_card"><div class="title"><div class="text">职位列表</div>
      <div class="search"><input type="text" id="search-text" placeholder="输入职位关键词">
      <input type="button" id="search-btn" value="搜索"></div></div><div class="pcards">''' + cards + '''</div></div>
      <script>
        window.applyClicks=0; window.expansionClicks=0; window.searchClicks=0;
        document.addEventListener('click',event=>{
          if(event.target.closest('.applybtn')){window.applyClicks++;event.preventDefault();return;}
          const card=event.target.closest('.position_card_li');
          if(card){
            window.expansionClicks++;
            const detail=card.querySelector(':scope > .detail');
            detail.style.display=detail.style.display==='none'?'block':'none';
            detail.innerHTML=__DETAIL__;
          }
        });
        document.querySelector('#search-btn').addEventListener('click',()=>{
          window.searchClicks++;
          for(const card of document.querySelectorAll('.position_card_li'))
            card.hidden=!card.querySelector('.pname').textContent.includes(document.querySelector('#search-text').value);
        });
      </script></body></html>'''.replace("__DETAIL__", json.dumps(DETAIL))


def must_reject(candidates, target=TARGET, candidate_id="") -> None:
    try:
        choose_candidate(candidates, "open_job", target, candidate_id)
    except ValueError:
        return
    raise AssertionError("ambiguous, absent or stale candidate was accepted")


async def run() -> None:
    assert job_identity(BASE + "&pid=47900") == "47900"
    assert job_identity(BASE + "&PID=47900") == "47900"
    assert job_identity(BASE + "&pid=47900&pid=47880") == ""
    assert job_identity("https://talent.autohome.com.cn/recruit-delivery.html?pid=47900") == "47900"
    assert job_identity("https://example.test/product?pid=47900") == ""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = await browser.new_context(service_workers="block")
            requests = []
            async def intercept(route):
                requests.append((route.request.method, route.request.url))
                await route.fulfill(body="<html><body></body></html>", content_type="text/html")
            await context.route("**/*", intercept)
            page = await context.new_page()
            await page.goto(BASE)
            await page.set_content(fixture(card("47900") + card("47880", city="上海") +
                card("47881", title="AI开发工程师【2026届校招】") + card("47882", hidden=True)))
            observed = await observe_navigation(page, TARGET)
            jobs = [item for item in observed["candidates"] if item.kind == "open_job"]
            assert len(jobs) == 3 and len({item.id for item in jobs}) == 3
            assert sum(item.matches_target for item in jobs) == 1
            assert all(item.url == "" and "申请" not in item.label for item in jobs)
            assert observed["heading"] == observed["job_id"] == ""
            chosen = choose_candidate(jobs, "open_job", TARGET)
            await execute_navigation(page, "open_job", TARGET, chosen.id)
            after = await observe_navigation(page, TARGET)
            assert after["heading"] == TITLE and after["job_id"] == "47900", after
            assert await page.evaluate("[window.applyClicks,window.expansionClicks]") == [0, 1]
            assert page.url == BASE
            must_reject(after["candidates"], candidate_id=chosen.id)
            for item in after["private"].values():
                assert await page.locator(f'[data-zhida-nav="{item["marker"]}"]').count() == 1

            # Identical titles stay distinct by DOM pid; never pick the first.
            await page.set_content(fixture(card("47900") + card("47901")))
            observed = await observe_navigation(page, TARGET)
            must_reject(observed["candidates"])
            target_pid = TARGET.model_copy(update={"source_url": BASE + "&pid=47901"})
            exact = await observe_navigation(page, target_pid)
            chosen = choose_candidate(exact["candidates"], "open_job", target_pid)
            assert exact["private"][chosen.id]["expansionPid"] == "47901"
            await page.locator('[pid="47901"].position_card_li').evaluate("el=>el.setAttribute('pid','49999')")
            changed = await observe_navigation(page, target_pid)
            must_reject(changed["candidates"], target_pid, chosen.id)
            assert await page.evaluate("window.applyClicks+window.expansionClicks") == 0

            # A URL locator, a hidden detail or several expanded jobs cannot
            # masquerade as a single loaded job detail.
            await page.goto(BASE + "&pid=47900")
            await page.set_content(fixture(card("47900")))
            assert (await observe_navigation(page))["job_id"] == ""
            await page.set_content(fixture(card("47900", expanded=True, hidden=True) + card("47901")))
            observed = await observe_navigation(page)
            assert observed["heading"] == observed["job_id"] == ""
            await page.set_content(fixture(card("47900", expanded=True) + card("47901", expanded=True)))
            observed = await observe_navigation(page)
            assert observed["heading"] == observed["job_id"] == ""

            # Real search control is an input[type=button], not a button tag.
            await page.goto(BASE)
            await page.set_content(fixture(card("47900") + card("47901", title="后端开发工程师【2027届校招】")))
            await execute_navigation(page, "search_jobs", TARGET)
            observed = await observe_navigation(page, TARGET)
            assert len([item for item in observed["candidates"] if item.kind == "open_job"]) == 1
            assert await page.evaluate("[window.searchClicks,window.applyClicks]") == [1, 0]

            # Neither arbitrary pid controls nor application destinations are
            # job-navigation controls, even with a pid or a misleading label.
            await page.set_content(fixture('''<div pid="47900">AI开发工程师</div>
              <div class="btn applybtn" pid="47900" role="button">申请该职位</div>
              <a href="recruit-delivery.html?pid=47900">查看详情</a>
              <a href="campus-recruit-list.html?pid=47900">Apply now</a>
              <button type="submit">提交申请</button>'''))
            assert not [item for item in (await observe_navigation(page))["candidates"] if item.kind == "open_job"]
            await page.goto("https://example.test/campus-recruit-list.html")
            await page.set_content(fixture(card("47900")))
            assert not [item for item in (await observe_navigation(page))["candidates"] if item.kind == "open_job"]
            assert all(method == "GET" for method, _ in requests), requests
        finally:
            await browser.close()
    print("autohome_navigation_regression_test: OK (pid cards, safe title expansion, search, target/city/cohort, ambiguity/stale/apply guards)")


if __name__ == "__main__":
    asyncio.run(run())
