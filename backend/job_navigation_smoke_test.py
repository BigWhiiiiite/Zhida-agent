"""Offline browser navigation regressions using intercepted synthetic pages only."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from playwright.async_api import async_playwright

from app.application_agent import decide_application_step
from app.application_models import ApplicationTarget, WorkflowAdvanceRequest
from app.browser_models import ExecutePlanRequest, FillAction
from app.browser_service import BrowserDemoService
from app.models import CandidateProfile


BASE = "https://campus.example.test"
TARGET = ApplicationTarget(company="示例科技", job_title="AI Agent 开发工程师", city="北京", recruitment_cycle="2027")
HOME = '''<!doctype html><html><title>示例科技 2027 校园招聘</title><body>
 <h1>示例科技 2027 校园招聘</h1><a href="/jobs">招聘职位</a>
 <a href="/#/page/投递简历">投递简历</a><button>提交简历</button></body></html>'''
LIST = '''<!doctype html><html><title>示例科技 2027 校招职位</title><body>
 <h1>示例科技 2027 校招职位</h1><input placeholder="搜索岗位关键词" id="search"
 oninput="document.getElementById('other').hidden=this.value.includes('Agent')">
 <ul><li><a href="/job/agent">AI Agent 开发工程师</a><span>北京</span></li>
 <li id="other"><a href="/job/other">后端开发工程师</a><span>上海</span></li></ul>
 <a href="/#/page/投递简历">投递简历</a></body></html>'''
DETAIL = '''<!doctype html><html><title>示例科技 AI Agent 开发工程师</title><body>
 <h1>AI Agent 开发工程师</h1><p>北京 2027 岗位职责：开发智能应用</p>
 <a href="/apply">立即申请</a></body></html>'''
FORM = '''<!doctype html><html><body><h1>在线申请表</h1>
 <form onsubmit="event.preventDefault();document.body.dataset.submitted='true'">
 <label>姓名<input name="full_name" required></label><label>邮箱<input name="email" required></label>
 <button type="submit">提交申请</button></form></body></html>'''


async def expect_error(call, text: str = "") -> None:
    try:
        await call
    except ValueError as error:
        assert not text or text in str(error), str(error)
    else:
        raise AssertionError("unsafe/no-progress operation unexpectedly succeeded")


async def run() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = await browser.new_context(service_workers="block")
            async def serve(route):
                path = route.request.url.split(BASE)[-1].split("?")[0]
                html = LIST if path == "/jobs" else DETAIL if path.startswith("/job/") else FORM if path == "/apply" else HOME
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
            await context.route("**/*", serve)
            page = await context.new_page()
            service = BrowserDemoService()
            service.page, service.context, service.session_id = page, context, "navigation-fixture"
            service.target = TARGET
            # Local fixture domains are intercepted; don't perform real DNS.
            with patch.object(service, "_validate_url", side_effect=lambda value: value):
                await page.goto(BASE)
                state = await service.workflow_state(service.session_id)
                assert state.stage == "homepage" and not state.final_submit_present
                assert not (await service.pre_submit_check(service.session_id)).ready
                await page.set_content('''<html><body><h1>简历预览</h1>
                    <button type="submit" onclick="document.body.dataset.submitted='yes'">投递简历</button></body></html>''')
                preview = await service.workflow_state(service.session_id)
                assert preview.stage == "unknown" and not preview.navigation_candidates
                await expect_error(service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent="browse_jobs")))
                await expect_error(service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent="start_application")))
                await expect_error(service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent="continue_application")))
                assert await page.locator("body").get_attribute("data-submitted") is None
                await page.goto(BASE)
                decision = await decide_application_step(state, await service.snapshot(), CandidateProfile(), use_model=False)
                assert decision.next_action == "browse_jobs" and decision.candidate_id
                state = await service.advance_workflow(service.session_id, WorkflowAdvanceRequest(
                    intent=decision.next_action,candidate_id=decision.candidate_id))
                assert state.stage == "job_list" and state.target.job_title == TARGET.job_title
                assert not (await service.pre_submit_check(service.session_id)).ready
                await expect_error(service.execute(service.session_id, ExecutePlanRequest(actions=[
                    FillAction(selector="#search",label="搜索",action="fill",value="不得填写本人资料",confidence=1)])), "不是可填写")
                decision = await decide_application_step(state, await service.snapshot(), CandidateProfile(), use_model=False)
                assert decision.next_action == "open_job", (decision.model_dump(),state.model_dump())
                state = await service.advance_workflow(service.session_id, WorkflowAdvanceRequest(
                    intent="open_job",candidate_id=decision.candidate_id))
                assert state.stage == "job_detail" and state.job_id == "agent"
                state = await service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent="start_application"))
                assert state.stage == "review" and state.form_fields == 2
                assert not (await service.pre_submit_check(service.session_id)).ready
                assert await page.locator("body").get_attribute("data-submitted") is None

                # A job search may only touch a verified search input and must
                # visibly change results, not count typing itself as progress.
                await page.goto(BASE + "/jobs")
                state = await service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent="search_jobs"))
                assert state.stage == "job_list"
                assert len([item for item in state.navigation_candidates if item.kind == "open_job"]) == 1
                await expect_error(service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent="search_jobs")), "未观察到")
                await expect_error(service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent="search_jobs")), "阻止重复")
                assert (await service.workflow_state(service.session_id)).navigation_blocker

                # Global CTA inside its own section: first no-op fails, retry
                # is blocked, and a changed/stale candidate ID never clicks.
                await page.goto(BASE + "/#/page/投递简历")
                await page.set_content('<html><body><h1>示例招聘</h1><a href="/#/page/投递简历">投递简历</a></body></html>')
                await expect_error(service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent="browse_jobs")), "未观察到")
                await expect_error(service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent="browse_jobs")), "阻止重复")
                await expect_error(service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent="browse_jobs",candidate_id="fabricated")), "候选")

                # Absent/ambiguous targets cannot auto-select the first result.
                await page.goto(BASE + "/jobs")
                service._stalled_navigation.clear()
                service.target = ApplicationTarget()
                await expect_error(service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent="open_job")), "唯一")
                service.target = TARGET
                await page.locator("ul").evaluate("el=>el.insertAdjacentHTML('beforeend','<li><a href=\"/job/agent2\">AI Agent 开发工程师</a><span>北京</span></li>')")
                await expect_error(service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent="open_job")), "唯一")
                chosen = next(item for item in (await service.workflow_state(service.session_id)).navigation_candidates if item.url.endswith('/job/agent2'))
                result = await service.advance_workflow(service.session_id, WorkflowAdvanceRequest(intent="open_job",candidate_id=chosen.id))
                assert result.job_id == "agent2"

                # Zero extracted controls never means 'all complete'.
                await page.goto(BASE + "/blank")
                await page.set_content('<html><body><button>提交简历</button></body></html>')
                assert (await service.workflow_state(service.session_id)).stage != "review"
                assert not (await service.pre_submit_check(service.session_id)).ready
        finally:
            await browser.close()
    print("job_navigation_smoke_test: OK (synthetic pages; homepage/list/detail/form, target, no-progress and zero-field guards)")


if __name__ == "__main__":
    asyncio.run(run())
