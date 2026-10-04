"""Intercepted, anonymous regressions for the public Beisen portal template.

Observed source: ux-recruitment-portal-2022/release/dist/
4934-1cbcf82f07bd91deec12.chk.js (STListItem/STJobTitle direct span)
recruitment-portal-176668749f2ea932925c.chk.js (Enter-driven nav search).
Every request is intercepted. No account, personal information or ATS write.
"""
from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright

from app.browser_models import ApplicationTarget
from app.application_models import WorkflowAdvanceRequest
from app.browser_service import BrowserDemoService
from app.ats_adapters import inspect_application_page, start_application
from app.job_navigation import (beisen_search_keyword, choose_candidate,
                                execute_navigation, observe_navigation, recruitment_cycle_matches)

BASE = "https://360campus.zhiye.com/campus/jobs"
TITLE = "27秋招-Agent智能体开发工程师（北京）-5386(J12456)"
TARGET = ApplicationTarget(company="奇虎360", job_title=TITLE, city="北京", recruitment_cycle="2027")


def card(title=TITLE, city="北京市", hidden=False):
    return f'''<div class="style__STListItem editor__sc-10r1nhd-0" {'hidden' if hidden else ''}>
      <div class="style__STListItemContent"><div class="style__STTitleSection">
        <div class="style__STJobTitle editor__sc-10r1nhd-4"><span>{title}</span></div>
        </div><div class="style__STOtherSection">{city}</div></div>
      <div><button class="apply">立即投递</button></div></div>'''


def fixture(cards):
    return '''<!doctype html><title>360集团校园招聘</title><body>
      <header>360集团 <input placeholder="搜索职位关键词"></header>
      <main>''' + cards + '''</main><form><button type="submit">提交申请</button></form>
      <script>
        window.titleClicks=0; window.applyClicks=0; window.submitClicks=0; window.searchKeys=0;
        for(const span of document.querySelectorAll('.style__STJobTitle > span'))
          span.onclick=()=>{window.titleClicks++;window.clickedTitle=span.textContent};
        for(const button of document.querySelectorAll('.apply')) button.onclick=()=>window.applyClicks++;
        document.querySelector('form').onsubmit=e=>{e.preventDefault();window.submitClicks++};
        document.querySelector('header input').onkeydown=e=>{
          if(e.key==='Enter'){
            window.searchKeys++;
            setTimeout(()=>{for(const card of document.querySelectorAll('.style__STListItem'))
              card.hidden=!card.textContent.includes(e.target.value)},900);
          }
        };
      </script></body>'''


def rejects(candidates, target=TARGET, candidate_id=""):
    try:
        choose_candidate(candidates, "open_job", target, candidate_id)
    except ValueError:
        return
    raise AssertionError("absent, ambiguous or stale job accepted")


async def run():
    assert beisen_search_keyword(TARGET) == "Agent智能体开发工程师"
    assert recruitment_cycle_matches("2027", "27秋招-Agent工程师")
    assert not recruitment_cycle_matches("2027", "26秋招-Agent工程师 2026-10-04")
    assert not recruitment_cycle_matches("2027", "薪资27万元")
    assert not recruitment_cycle_matches("2027春招", "27秋招-Agent工程师")
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = await browser.new_context(service_workers="block")
            requests = []
            async def intercept(route):
                requests.append((route.request.method, route.request.url))
                await route.fulfill(body="<html></html>", content_type="text/html")
            await context.route("**/*", intercept)
            page = await context.new_page()
            await page.goto(BASE)
            other = "27秋招-后端开发工程师（北京）-5000(J12000)"
            await page.set_content(fixture(card() + card(other) + card(hidden=True)))
            observed = await observe_navigation(page, TARGET)
            jobs = [item for item in observed["candidates"] if item.kind == "open_job"]
            assert len(jobs) == 2 and sum(item.matches_target for item in jobs) == 1, jobs
            assert all(not item.url for item in jobs)
            chosen = choose_candidate(jobs, "open_job", TARGET)
            await execute_navigation(page, "open_job", TARGET, chosen.id)
            assert await page.evaluate("[titleClicks,applyClicks,submitClicks]") == [1, 0, 0]
            assert await page.evaluate("clickedTitle") == TITLE
            # Changed provenance invalidates the candidate, not an index click.
            await page.locator('.style__STOtherSection').first.evaluate("el=>el.textContent='上海市'")
            changed = await observe_navigation(page, TARGET)
            rejects(changed["candidates"], candidate_id=chosen.id)
            assert not any(item.matches_target for item in changed["candidates"])

            await page.set_content(fixture(card() + card()))
            duplicate = await observe_navigation(page, TARGET)
            jobs = [item for item in duplicate["candidates"] if item.kind == "open_job"]
            assert len(jobs) == 2 and jobs[0].id != jobs[1].id
            rejects(jobs)
            try:
                await execute_navigation(page, "open_job", TARGET, jobs[0].id)
            except ValueError as exc:
                assert "完全相同" in str(exc)
            else:
                raise AssertionError("indistinguishable duplicate clicked")

            # The owning card, not a neighbouring card/year, establishes cohort.
            await page.set_content(fixture(card(TITLE.replace('27秋招', '26秋招')) + card(other)))
            broad = TARGET.model_copy(update={"job_title": "Agent智能体开发工程师"})
            rejects((await observe_navigation(page, broad))["candidates"], broad)

            await page.set_content(fixture(card() + card(other)))
            await execute_navigation(page, "search_jobs", TARGET)
            await page.wait_for_timeout(500)
            result = await observe_navigation(page, TARGET)
            assert len([item for item in result["candidates"] if item.kind == "open_job"]) == 1
            assert await page.locator('header input').input_value() == "Agent智能体开发工程师"
            assert await page.evaluate("[searchKeys,applyClicks,submitClicks]") == [1, 0, 0]

            # Service waits for the delayed result instead of treating typing
            # as progress or repeating Enter. Unchanged results still stop.
            await page.set_content(fixture(card() + card(other)))
            service = BrowserDemoService()
            service.page, service.context, service.session_id = page, context, 'beisen-navigation-fixture'
            service.target = TARGET
            state = await service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent='search_jobs'))
            assert len([item for item in state.navigation_candidates if item.kind == 'open_job']) == 1
            assert not service._stalled_navigation
            for message in ['未观察到', '阻止重复']:
                try:
                    await service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent='search_jobs'))
                except ValueError as exc:
                    assert message in str(exc), str(exc)
                else:
                    raise AssertionError('no-progress search unexpectedly succeeded')
            assert await page.evaluate('[searchKeys,applyClicks,submitClicks]') == [2, 0, 0]

            # Same template on another tenant, not hard-coded to 360/J12456.
            await page.goto('https://example.zhiye.com/campus/jobs')
            await page.set_content(fixture(card()).replace('360集团', '示例科技'))
            tenant = TARGET.model_copy(update={"company": "示例科技"})
            assert choose_candidate((await observe_navigation(page, tenant))["candidates"], "open_job", tenant)
            # No Enter in a form, a detail page or an unrelated host.
            for url, nested in [(BASE, True), ('https://example.test/campus/jobs', False),
                                ('https://example.zhiye.com/form', False)]:
                await page.goto(url)
                await page.set_content('''<body><form><button type="submit">提交</button></form>
                  <input placeholder="搜索职位关键词"><script>window.keys=0;
                  document.querySelector('input').onkeydown=e=>{if(e.key==='Enter')window.keys++}</script></body>''')
                if nested:
                    await page.locator('input').evaluate("el=>document.querySelector('form').append(el)")
                await execute_navigation(page, "search_jobs", TARGET)
                assert await page.evaluate('keys') == 0
            assert all(method == "GET" for method, _ in requests)

            detail = '''<title>360集团校园招聘</title><header><input placeholder="搜索职位关键词"></header>
              <main class="style__STComponent editor__sc-ydltt0-9">
              <div class="style__STJobNameLeft editor__sc-ydltt0-19"><span>__TITLE__</span></div>
              <div><div class="style__STDutyTitle">岗位职责</div><div class="style__STDutyContent"><p>研发智能体应用和评估系统。</p></div></div>
              <button>立即投递</button></main>'''.replace('__TITLE__', TITLE)
            await page.goto('https://360campus.zhiye.com/campus/detail?jobAdId=fixture-detail')
            await page.set_content(detail)
            state = await inspect_application_page(page, 'fixture', TARGET)
            assert state.stage == 'job_detail' and state.job_title == TITLE, state.model_dump()
            for html in [detail.replace('研发智能体应用和评估系统。', '加载中'),
                         detail.replace('style__STComponent editor__sc-ydltt0-9', 'recommend-card'),
                         detail.replace('style__STJobNameLeft editor__sc-ydltt0-19', 'style__STJobNameLeft editor__sc-ydltt0-19" style="display:none')]:
                await page.set_content(html)
                assert (await inspect_application_page(page, 'fixture', TARGET)).stage != 'job_detail'

            # Bare div '投递' qualifies only in the known, loaded detail slot.
            portal = detail.replace('<button>立即投递</button>', '''<div class="style__STDeliverBtn editor__sc-ydltt0-30"
              onclick="location.href='/login?goto=campus/detail'"><div><span>投递</span></div></div>''')
            await page.set_content(portal)
            assert (await inspect_application_page(page, 'fixture', TARGET)).stage == 'job_detail'
            await start_application(page, TARGET)
            assert '/login?' in page.url
            await page.goto('https://360campus.zhiye.com/campus/detail?jobAdId=fixture-detail')
            for html in [portal.replace('>投递<', '>提交申请<'),
                         portal.replace('<div><span>投递', '<div aria-disabled="true"><span>投递'),
                         portal.replace('<div><span>投递', '<div><input name="name"><span>投递'),
                         portal.replace('</main>', '<div class="style__STDeliverBtn">投递</div></main>'),
                         portal.replace('style__STDeliverBtn editor__sc-ydltt0-30', 'arbitrary-cta')]:
                await page.set_content(html)
                assert (await inspect_application_page(page, 'fixture', TARGET)).stage != 'job_detail'
                try:
                    await start_application(page, TARGET)
                except ValueError:
                    pass
                else:
                    raise AssertionError('ambiguous/final/disabled/form/arbitrary entry accepted')
            await page.goto('https://360campus.zhiye.com/form?jobAdId=fixture-detail')
            await page.set_content(portal)
            assert (await inspect_application_page(page, 'fixture', TARGET)).stage != 'job_detail'
            # Passwordless identifier-first login: no password/OTP yet and
            # Next may be a non-semantic div. This is a human gate, not a form.
            await page.goto('https://360campus.zhiye.com/login?goto=campus/detail')
            login = '''<header><input placeholder="搜索职位关键词"></header><main>
              <h2>登录/注册</h2><label>手机号<input name="phone" placeholder="请输入手机号"></label>
              <label><input type="checkbox">我已阅读并同意隐私政策</label><div>下一步</div></main>'''
            await page.set_content(login)
            state = await inspect_application_page(page, 'fixture', TARGET)
            assert state.stage == 'auth_required' and not state.authenticated and state.requires_consent
            assert {action.intent for action in state.actions} == {'manual_login', 'refresh'}
            assert not await page.locator('input[type="checkbox"]').is_checked()
            for html in [login.replace('登录/注册', '加载中'), '<main>登录/注册</main>']:
                await page.set_content(html)
                # Visible privacy requires handoff even before auth text or
                # identifier appears; a bare URL/caption still does not.
                assert (await inspect_application_page(page, 'fixture', TARGET)).stage == ('auth_required' if 'checkbox' in html else 'unknown')
            await page.set_content('<main><label><input type="checkbox">我已阅读并同意《北森隐私政策》</label></main>')
            state = await inspect_application_page(page, 'fixture', TARGET)
            assert state.stage == 'auth_required' and state.requires_consent
            await page.goto('https://360campus.zhiye.com/campus/jobs')
            assert (await inspect_application_page(page, 'fixture', TARGET)).stage != 'auth_required'
        finally:
            await browser.close()
    print("beisen navigation: title-only click, Enter search, tenant/cohort/city, duplicates, stale and submit guards passed")


if __name__ == "__main__":
    asyncio.run(run())
