"""Isolated synthetic portal tests. Never attach to real browser/user state."""
import asyncio

from playwright.async_api import async_playwright

from app.browser_models import ApplicationTarget
from app.job_navigation import choose_candidate, execute_navigation, observe_navigation


BASE = "https://organization-fixture.example.test"
HTML = '''<!doctype html><title>示例银行 2027 校园招聘</title>
<header><a href="/campus">校园招聘</a></header>
<section><h2>招聘机构</h2>
  <div style="cursor:pointer" onclick="document.querySelector('#jobs').hidden=false;this.closest('section').hidden=true">
    <h3>总行</h3><span>→</span></div>
  <a href="/branches"><h3>分行</h3><span>→</span></a>
  <div role="button" onclick="document.body.dataset.choice='subsidiary'"><h3>子公司</h3></div>
  <button disabled>总部招聘</button>
  <div aria-hidden="true"><button>海外分行</button></div>
  <div style="cursor:pointer"><h3>北京分公司</h3><button>提交申请</button></div>
  <div style="cursor:pointer"><h3>上海分行</h3><a href="/one">一</a><a href="/two">二</a></div>
</section>
<aside><button onclick="document.body.dataset.privateClick='yes'">总行</button></aside>
<section id="jobs" hidden><h2>校招职位</h2><a href="/jobs/agent">人工智能方向管理培训生</a><p>北京</p></section>'''


async def run():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = await browser.new_context(service_workers="block")
            # No website, backend, model or private profile is ever requested.
            await context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=HTML))
            page = await context.new_page()
            await page.goto(BASE + "/campus")
            observed = await observe_navigation(page)
            candidates = observed["candidates"]
            assert [item.label for item in candidates] == ["分行", "子公司", "总行"], [item.model_dump() for item in candidates]
            assert all(item.entry_scope == "organization" and item.requires_user_choice for item in candidates)
            assert "校园招聘" not in [item.label for item in candidates], "Current-page header is not a new entrance"
            try:
                choose_candidate(candidates, "browse_jobs", ApplicationTarget())
                raise AssertionError("Organization preference was guessed")
            except ValueError:
                pass
            # Explicit choice uses a fresh observed marker and leaves other
            # organizations/submission controls untouched. No URL invented.
            head = next(item for item in candidates if item.label == "总行")
            assert head.url == ""
            await execute_navigation(page, "browse_jobs", ApplicationTarget(), head.id)
            after = await observe_navigation(page)
            assert any(item.kind == "open_job" and item.label == "人工智能方向管理培训生" for item in after["candidates"])
            assert await page.locator("body").get_attribute("data-private-click") is None
            assert await page.locator("body").get_attribute("data-choice") is None
            # No recruiting section = no arbitrary corporate-card clicks.
            await page.set_content('<h2>关于我们</h2><button>总行</button><div style="cursor:pointer">分公司</div>')
            assert not (await observe_navigation(page))["candidates"]
            await page.set_content('<section><h2>招聘机构</h2><button>总行</button></section>')
            single = (await observe_navigation(page))["candidates"]
            assert len(single) == 1, [item.model_dump() for item in single]
            try:
                choose_candidate(single, "browse_jobs", ApplicationTarget())
                raise AssertionError("Single organization is not implicit preference consent")
            except ValueError:
                pass
            await page.set_content('<section><h2>招聘机构</h2><div><h3>北京分行</h3><a href="/observed-branch">→</a></div></section>')
            arrow = (await observe_navigation(page))["candidates"]
            assert len(arrow) == 1 and arrow[0].label == "北京分行" and arrow[0].url == BASE + "/observed-branch"
            await page.set_content('<form id="real-apply"></form><section><h2>招聘机构</h2><button form="real-apply">总行</button><button type="submit">分行</button></section>')
            assert not (await observe_navigation(page))["candidates"], "Form-associated or explicit submit controls cannot become entrances"
        finally:
            await browser.close()
    print("organization_navigation_test: OK (observed cards, same-page header, explicit choices, hidden/disabled/unsafe controls)")


if __name__ == "__main__":
    asyncio.run(run())
