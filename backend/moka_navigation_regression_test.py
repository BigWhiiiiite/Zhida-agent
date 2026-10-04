"""Real Chromium, fully intercepted synthetic ATS pages; no external writes."""
import asyncio

from playwright.async_api import async_playwright
from app.ats_adapters import inspect_application_page
from app.browser_models import ApplicationTarget, NavigationCandidate
from app.job_navigation import choose_candidate, observe_navigation


async def run():
    target = ApplicationTarget(recruitment_cycle='2027校招')
    social = NavigationCandidate(id='social', label='加入我们',
        url='https://app.mokahr.com/apply/fixture/1#/jobs', kind='browse_jobs')
    try:
        choose_candidate([social], 'browse_jobs', target)
        raise AssertionError('campus must not automatically switch to generic/social site')
    except ValueError:
        pass
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            await context.route('**/*', lambda r: r.fulfill(body='<html><body></body></html>', content_type='text/html'))
            page = await context.new_page()
            await page.goto('https://app.mokahr.com/apply/fixture/1#/jobs')
            await page.set_content('''<h1>校园招聘</h1><input placeholder="搜索地点关键词">
                <input placeholder="搜索岗位关键词"><a href="#/job/agent"><h3>AI Agent开发工程师</h3>
                <p>北京 岗位描述：''' + '岗位职责说明。' * 100 + '''</p></a>''')
            observed = await observe_navigation(page)
            assert observed['heading'] == ''
            assert len([c for c in observed['candidates'] if c.kind == 'search_jobs']) == 1
            assert len([c for c in observed['candidates'] if c.kind == 'open_job']) == 1
            state = await inspect_application_page(page, 'fixture')
            assert state.stage == 'job_list', state.model_dump()
            assert not state.job_title
            await page.goto('https://app.mokahr.com/campus-recruitment/fixture/1#/job/agent')
            await page.set_content('''<header><h1>校园招聘</h1></header><main>
                <div class="job-name_ab123">AI Agent开发工程师</div><p>岗位职责：开发智能体；任职要求：Python。</p>
                <button>申请职位</button></main>''')
            state = await inspect_application_page(page, 'fixture')
            assert state.stage == 'job_detail' and state.job_title == 'AI Agent开发工程师', state.model_dump()
            assert '开发智能体' in state.page_evidence
            assert not state.authenticated
        finally:
            await browser.close()
    print('moka_navigation_regression_test: OK (campus guard, hash route, long cards, city search, visible title)')


if __name__ == '__main__':
    asyncio.run(run())
