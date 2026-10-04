"""Anonymous, wholly intercepted browser regression; no website/model/DB access.

The fixture captures template shapes, not a copy of a live recruitment page.
Every request is fulfilled locally; service workers and sockets are blocked.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from app.application_models import ApplicationTarget
from app.ats_adapters import inspect_application_page, start_application, fill_verification_code


BASE = "https://fixture.zhiye.com"
URL = BASE + "/custom/rsdetail?hideAll=1&jc=2&jobAdId=offline-job"
HTML = Path(__file__).with_name("fixtures").joinpath("beisen_custom_journey.html").read_text()
TITLE = "实验中心-系统研发岗（2027校招）(T10001)"
AUTH = '''<main><h1>手机验证码登录</h1><label>手机号<input name="phone"></label>
<label>短信验证码<input autocomplete="one-time-code"></label>
<button type="button" onclick="location.href='/form?jobAdId=offline-job'">登录</button></main>'''
FORM = '''<header><a href="/login">登录</a><a href="/account">个人中心</a></header>
<main><h1>你正在投递职位：实验中心-系统研发岗（2027校招）(T10001)</h1>
<h2>个人信息</h2><label>姓名<input></label><label>邮箱<input></label>
<h2>教育经历</h2><label>学校<input></label>
<label><input type="checkbox">我承诺以上信息真实</label>
<button type="submit" onclick="document.body.dataset.final='yes'">提交申请</button></main>'''


async def expect_rejected(call):
    try:
        await call
    except (ValueError, LookupError):
        return
    raise AssertionError("unsafe entry unexpectedly accepted")


async def run():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = await browser.new_context(service_workers="block")
            requests = []

            async def serve(route):
                requests.append(route.request.url)
                path = urlparse(route.request.url).path
                body = AUTH if path == "/login" else FORM if path == "/form" else HTML
                await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=body)

            await context.route("**/*", serve)
            await context.route_web_socket("**/*", lambda socket: socket.close())
            page = await context.new_page()

            async def observe(html=HTML, url=URL):
                await page.goto(url)
                await page.set_content(html)
                return await inspect_application_page(page, "anonymous-journey", ApplicationTarget(job_title="目标不会替代网页标题"))

            # Multiple custom route/title layouts use real DOM ownership, not
            # document.title, hardcoded company/job or URL-target interpolation.
            for route in ("rsdetail", "campusdetail", "socialdetail"):
                for title_shape in ("detail-title", "title", "anonymous-title"):
                    state = await observe(HTML.replace('class="detail-title"', f'class="{title_shape}"'),
                                          BASE + f"/custom/{route}?jobAdId=offline-job")
                    assert state.stage == "job_detail" and state.job_title == TITLE, state.model_dump()
                    assert not state.authenticated
                    assert {action.intent for action in state.actions} == {"start_application"}
            other = HTML.replace(TITLE, "匿名应用开发工程师（2028校招）(T20002)")
            state = await observe(other)
            assert state.stage == "job_detail" and state.job_title.endswith("(T20002)")
            await start_application(page)
            assert urlparse(page.url).path == "/login"

            # Complete product-owned navigation boundary: detail -> OTP gate ->
            # human-provided code -> editable form, with no final submission.
            await observe()
            await start_application(page)
            otp = await inspect_application_page(page, "anonymous-journey")
            assert otp.stage == "verification_required" and not otp.authenticated
            await fill_verification_code(page, "000000", submit=True)
            form = await inspect_application_page(page, "anonymous-journey")
            assert form.stage == "review" and form.authenticated and form.job_title == TITLE
            assert {action.intent for action in form.actions} == {"refresh"}
            assert not await page.locator('input[type="checkbox"]').is_checked()
            assert await page.locator("body").get_attribute("data-final") is None
            await expect_rejected(start_application(page))

            # Header Login alone is neither a gate nor proof of an account.
            logged_in = await observe(HTML.replace('</header>', '<a href="/account">个人中心</a></header>'))
            assert logged_in.stage == "job_detail" and logged_in.authenticated
            auth = await observe('<header><input type="search" placeholder="搜索岗位"></header>'
                '<main><h1>登录</h1><button>登录</button><p>微信扫码登录</p></main>',
                BASE + '/login?jobAdId=offline-job&goto=%2Fform')
            assert auth.stage == "auth_required", auth.model_dump()
            stale_auth = await observe('<a href="/account">个人中心</a><main><h1>登录</h1>'
                '<label>手机号<input name="phone"></label><input type="password"><button>登录</button></main>',
                BASE + '/login?jobAdId=offline-job')
            assert stale_auth.stage == "auth_required" and not stale_auth.authenticated

            # Only document title, only generic headings, hidden/recommended
            # titles, or a skeleton must never unlock #apply or classify search
            # controls as an application form.
            negatives = [
                HTML.replace('<div class="detail-title">' + TITLE + '</div>', ''),
                HTML.replace('<div class="detail-title">', '<div hidden class="detail-title">'),
                HTML.replace('class="detail-title"', 'class="detail-title recommend-card"'),
                HTML.replace(TITLE + '</div>', '职位详情</div>'),
                HTML.replace('开发可验证的软件服务和业务应用。', '加载中…')
                    .replace('具备计算机基础、程序设计能力和团队协作能力。', '加载中…'),
                '<title>' + TITLE + '</title><header><input type="search" placeholder="搜索职位"><a href="/login">登录</a></header>'
                    '<main><div id="apply">立即申请</div><p>加载中…</p></main>',
            ]
            for html in negatives:
                state = await observe(html)
                assert state.stage == "unknown", state.model_dump()
                assert not any(action.intent == "start_application" for action in state.actions)
                await expect_rejected(start_application(page))
            no_id = await observe(negatives[-1], BASE + '/custom/newdetail?jobAdId=')
            assert no_id.stage == "unknown" and not no_id.job_title

            # ID is not click authority. No duplicate IDs, fake final wording,
            # preview controls, submit-type nodes or arbitrary div fallback.
            unsafe = [
                HTML.replace('</main>', '<div id="apply">立即申请</div></main>'),
                HTML.replace('>立即申请</div>', '>提交申请</div>'),
                HTML.replace('id="apply"', 'id="unknown-apply"'),
                HTML.replace('<div id="apply"', '<button type="submit" id="apply"').replace('>立即申请</div>', '>立即申请</button>'),
                HTML.replace('<div id="apply"', '<div aria-disabled="true" id="apply"'),
                HTML.replace('</main>', '<label>姓名<input name="full_name"></label></main>'),
            ]
            for html in unsafe:
                await observe(html)
                await expect_rejected(start_application(page))
                assert urlparse(page.url).path == '/custom/rsdetail'
            ambiguous = HTML.replace('</main>', '<button>立即申请</button><button>立即申请</button></main>')
            await observe(ambiguous)
            await expect_rejected(start_application(page))

            # A login modal wins over both stale account evidence and JD.
            modal = await observe(HTML + '<div role="dialog"><h2>重新登录</h2><input type="password"><button>登录</button></div>')
            assert modal.stage == "auth_required" and not modal.authenticated
            await expect_rejected(start_application(page))
            arbitrary = await observe('<label>任意文字<input></label><button>下一步</button>', BASE + '/misc')
            assert arbitrary.stage == "unknown"
            assert all(url.startswith(BASE) for url in requests), requests
        finally:
            await browser.close()
    print("beisen_custom_journey_test: OK (offline custom templates, title grounding, entry/auth/form, no submit, negative gates)")


if __name__ == "__main__":
    asyncio.run(run())
