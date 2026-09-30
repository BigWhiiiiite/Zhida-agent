"""Offline Beisen stage regressions; every browser request is intercepted.

No real recruitment page, login, application, profile or model is accessed.
"""
from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from app.ats_adapters import inspect_application_page
from app.job_navigation import job_identity


BASE = "https://beisen.zhiye.com"
JOB_ID = "8f54d78f-10b3-4de3-b1fa-17e756ed6846"
DETAIL_URL = BASE + "/campus/detail/detail?jobAdId=" + JOB_ID
FORM_URL = BASE + "/form?fromPage=job&jobAdId=" + JOB_ID + "&userId=synthetic-only-user"
APPLICATION_TITLE = "2027届校招-AI算法工程师（北京）(J14872)"
APPLICATION_HEADING = '<div class="application-header">你正在投递职位：<span>' + APPLICATION_TITLE + '</span></div>'
NAV = '''<header><h1>招聘官网</h1><input type="search" placeholder="搜索职位关键词">
  <button>搜索</button><a href="/login">登录</a></header>'''
DETAIL = NAV + '''<main class="jobDetail"><div class="jobTitle">AI Agent 开发工程师</div>
  <p>工作地点：北京 · 2027校园招聘</p><section><h2>工作职责</h2>
  <p>开发面向企业业务场景的智能体应用，并设计可验证的工具调用流程。</p></section>
  <h2>任职资格</h2><p>熟悉 Python 与服务端工程开发。</p>
  <button onclick="document.body.dataset.applied='true'">立即申请</button></main>'''
APPLICATION_FORM = NAV + APPLICATION_HEADING + '''<main><form>
  <h2>上传简历</h2><h2>投递意向</h2><h2>个人信息</h2>
  <div class="custom-row"><div>姓名</div><div><input name="f001" placeholder="请输入"></div></div>
  <div class="custom-row"><div>出生日期</div><div><input name="f002" placeholder="请选择"></div></div>
  <div class="custom-row"><div>邮箱</div><div><input name="f003" placeholder="请输入"></div></div>
  <div class="custom-row"><div>手机号</div><div><input name="f004" placeholder="请输入"></div></div>
  <h2>教育经历</h2><button type="button">保存草稿</button>
  </form></main>'''


def identity_checks() -> None:
    for query in ("jobAdId", "JOBADID", "jobadid", "job_ad_id", "postid", "jobId", "job_id", "positionId"):
        assert job_identity(BASE + "/campus/detail/detail?" + query + "=" + JOB_ID) == JOB_ID
    assert job_identity(BASE + "/#/campus/detail?jobAdId=" + JOB_ID) == JOB_ID
    assert job_identity(BASE + "/campus/detail/detail?jobAdId=&tracking=abc") == ""
    assert job_identity(BASE + "/campus/detail/detail?jobAdId=a&jobAdId=b") == ""
    assert job_identity(BASE + "/campus/detail/detail?jobAdId=a&jobId=b") == ""
    assert job_identity(BASE + "/campus/detail/detail?jobAdId=a&jobadid=a") == "a"
    assert job_identity(BASE + "/campus/detail/detail?jobAdId=%2Funsafe%2Fvalue") == ""
    assert job_identity(BASE + "/jobs/list?keyword=agent") == ""
    assert job_identity("https://app.mokahr.com/campus-recruitment/fixture/1#/job/fixture-job") == "fixture-job"


async def run() -> None:
    identity_checks()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = await browser.new_context(service_workers="block")
            await context.route("**/*", lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body="<html><body></body></html>"))
            page = await context.new_page()

            async def inspect(html: str, url: str = DETAIL_URL):
                await page.goto(url)
                await page.set_content("<!doctype html><html><body>" + html + "</body></html>")
                result = await inspect_application_page(page, "beisen-fixture")
                assert await page.locator("body").get_attribute("data-applied") is None
                return result

            # A valid URL and top-bar search/login are not a loaded job or an
            # authentication requirement. No browser navigation action offered.
            shell = await inspect(NAV + '<main aria-busy="true">加载中…</main>')
            assert shell.adapter == "beisen-italent" and shell.job_id == JOB_ID
            assert shell.stage == "unknown", shell.model_dump()
            assert not shell.job_title and not shell.authenticated
            assert "尚未读取" in shell.message and "不代表岗位列表" in shell.message
            assert {action.intent for action in shell.actions} == {"refresh"}
            assert any(item.kind == "search_jobs" for item in shell.navigation_candidates)

            # A placeholder Apply CTA does not turn the same shell into a job.
            incomplete = await inspect(NAV + '<h1>职位详情</h1><button>立即申请</button>')
            assert incomplete.stage == "unknown" and not incomplete.job_title
            assert not any(action.intent == "start_application" for action in incomplete.actions)

            # Missing/invalid ID on a known detail route stays unknown, not list.
            missing_id = await inspect(NAV, BASE + "/campus/detail/detail?jobAdId=")
            assert missing_id.stage == "unknown" and not missing_id.job_id

            # Correctly loaded detail wins over the persistent global search.
            detail = await inspect(DETAIL)
            assert detail.stage == "job_detail", detail.model_dump()
            assert detail.job_title == "AI Agent 开发工程师"
            assert not detail.authenticated
            assert {action.intent for action in detail.actions} == {"start_application"}
            assert "已读取可见岗位标题及职责/要求正文" in detail.stage_evidence

            # Other known visible title shapes work, hidden/recommended ones do
            # not replace the primary job title.
            for title_class in ("jobName", "positionTitle", "position-name", "post_title"):
                loaded = await inspect(DETAIL.replace('class="jobTitle"', f'class="{title_class}"') +
                    '<div hidden class="jobTitle">错误隐藏标题</div>')
                assert loaded.job_title == "AI Agent 开发工程师" and loaded.stage == "job_detail"
            card_only = await inspect(NAV + '<article class="job-card"><div class="jobTitle">推荐岗位</div>'
                '<p>岗位职责：这只是推荐卡片，不是详情。</p></article><button>立即申请</button>')
            assert card_only.stage == "unknown" and not card_only.job_title

            # Title/body without an actual application entry is not ready.
            no_entry = await inspect(DETAIL.replace('>立即申请</button>', '>收藏职位</button>'))
            assert no_entry.stage == "unknown" and "申请入口" in no_entry.message
            assert {action.intent for action in no_entry.actions} == {"refresh"}

            # A real list page remains a list and its jobAdId links are usable
            # observed candidates (not invented from a target).
            listing = await inspect(NAV + '<h1>校园招聘</h1><ul><li><a href="' +
                DETAIL_URL + '">AI Agent 开发工程师</a><span>北京</span></li></ul>', BASE + "/campus/jobs")
            assert listing.stage == "job_list" and not listing.job_id
            assert any(item.kind == "open_job" and item.url == DETAIL_URL for item in listing.navigation_candidates)

            # Explicit missing/closed-job content forbids apply even if stale
            # layout and button remain mounted below the error message.
            for notice in ("职位不存在", "该职位已下线", "岗位不存在", "职位已删除", "岗位已下架"):
                missing = await inspect('<div role="alert">' + notice + '</div>' + DETAIL)
                assert missing.stage == "unknown" and "关闭或下线" in missing.message
                assert {action.intent for action in missing.actions} == {"refresh"}

            # A visible auth modal takes precedence over loaded detail and an
            # account menu left in the background after a session expires.
            auth = await inspect(DETAIL + '<a href="/account">个人中心</a>'
                '<div role="dialog" aria-modal="true"><h2>登录</h2>'
                '<label>手机号<input name="phone"></label>'
                '<label>密码<input type="password"></label><button>登录</button></div>')
            assert auth.stage == "auth_required" and not auth.authenticated, auth.model_dump()
            assert not any(action.intent == "start_application" for action in auth.actions)
            otp = await inspect(DETAIL + '<div role="dialog"><h2>手机登录</h2>'
                '<input autocomplete="one-time-code" placeholder="短信验证码"><button>登录</button></div>')
            assert otp.stage == "verification_required" and not otp.authenticated
            qr = await inspect(DETAIL + '<div role="dialog"><h2>微信扫码登录</h2><p>扫描二维码继续</p></div>')
            assert qr.stage == "auth_required"
            hidden = await inspect(DETAIL + '<div role="dialog" style="visibility:hidden">'
                '<input type="password"><button>登录</button></div>')
            assert hidden.stage == "job_detail"

            # jobAdId survives the JD -> application transition. A form with
            # custom sibling captions and generic placeholders needs no JD.
            form = await inspect(APPLICATION_FORM, FORM_URL)
            assert form.stage == "application_form", form.model_dump()
            assert form.job_id == JOB_ID and form.job_title == APPLICATION_TITLE
            assert form.form_fields == 4 and not form.authenticated
            assert "详情" not in form.message and "登录" not in form.message
            assert {action.intent for action in form.actions} == {"analyze_form"}
            assert "已读取申请页的个人资料模块及题目标题" in form.stage_evidence
            # The visible title is observed independently from a user target,
            # and form evidence also works without userId/jobAdId parameters.
            no_ids = await inspect(APPLICATION_FORM, BASE + "/form")
            assert no_ids.stage == "application_form" and no_ids.job_title == APPLICATION_TITLE
            assert not no_ids.authenticated and not no_ids.job_id
            with_logout = await inspect(APPLICATION_FORM + '<a href="/logout">退出登录</a>', FORM_URL)
            assert with_logout.stage == "application_form" and with_logout.authenticated
            assert "退出登录" in with_logout.authentication_evidence
            # Structural evidence can recognize a genuine form even when no
            # one-to-one sibling label is obtainable (classification only).
            complex_form = NAV + APPLICATION_HEADING + '<main><h2>个人信息</h2><h2>教育经历</h2>'
            complex_form += '<div><span>姓名</span><span>邮箱</span><span>手机号</span>'
            complex_form += '<input placeholder="请输入"><input placeholder="请输入"><input placeholder="请输入"></div></main>'
            structural = await inspect(complex_form, FORM_URL)
            assert structural.stage == "application_form" and structural.form_fields == 3

            # The exact URL, userId, application title and module/question
            # captions are insufficient without real editable profile fields.
            readonly = await inspect(NAV + APPLICATION_HEADING +
                '<h2>个人信息</h2><h2>教育经历</h2><span>姓名</span><span>邮箱</span><span>手机号</span>', FORM_URL)
            assert readonly.stage == "unknown" and not readonly.authenticated
            assert readonly.job_title == APPLICATION_TITLE and readonly.form_fields == 0
            assert "尚未检测到可填写" in readonly.message and "职责" not in readonly.message
            assert {action.intent for action in readonly.actions} == {"refresh"}
            unlabeled = await inspect(NAV + APPLICATION_HEADING + '<input placeholder="请输入">', FORM_URL)
            assert unlabeled.stage == "unknown" and not unlabeled.authenticated

            # Active authentication still overrides a genuine application
            # form and a stale logout link. Never fill or submit during tests.
            form_auth = await inspect(APPLICATION_FORM + '<a href="/logout">退出登录</a>'
                '<div role="dialog"><h2>重新登录</h2><input type="password"><button>登录</button></div>', FORM_URL)
            assert form_auth.stage == "auth_required" and not form_auth.authenticated
            assert not any(action.intent in {"analyze_form", "start_application"} for action in form_auth.actions)

            # A one-question step is genuine when all independent signals
            # exist: owned profile label + valid progress + safe continuation.
            one_step = '<main><p>Step 1 of 2</p><label>Name<input name="f001" required></label>'
            one_step += '<button>Save and continue</button></main>'
            step = await inspect(one_step, FORM_URL)
            assert step.stage == "application_form" and step.form_fields == 1
            assert (step.page_step_current, step.page_step_total) == (1, 2)
            assert step.safe_next_present and not step.authenticated
            assert "已识别明确步骤进度、个人资料题目及安全下一步" in step.stage_evidence
            # Never use just a route, arbitrary/opaque input, metadata name,
            # search box, progress counter or a final-submit CTA as this proof.
            step_negatives = [
                one_step.replace('<p>Step 1 of 2</p>', ''),
                one_step.replace('<button>Save and continue</button>', ''),
                one_step.replace('<button>Save and continue</button>', '<button>提交申请</button>'),
                one_step.replace('Step 1 of 2', 'Step 2 of 2'),
                one_step.replace('<label>Name<input name="f001" required></label>', '<input name="name" placeholder="请输入">'),
                one_step.replace('<label>Name<input name="f001" required></label>', '<label>任意内容<input name="f001"></label>'),
                one_step.replace('<label>Name<input name="f001" required></label>', '<input type="search" placeholder="搜索职位">'),
                one_step.replace('<label>Name<input name="f001" required></label>', '<label>Name<input name="f001" disabled></label>'),
            ]
            for html in step_negatives:
                rejected = await inspect(html, FORM_URL)
                assert rejected.stage == "unknown", rejected.model_dump()
                assert not any(action.intent in {"analyze_form", "continue_application"} for action in rejected.actions)
            stepped_auth = await inspect(one_step + '<div role="dialog"><input type="password"><button>登录</button></div>', FORM_URL)
            assert stepped_auth.stage == "auth_required"
        finally:
            await browser.close()
    print("beisen_stage_smoke_test: OK (offline URL identity, SPA shell, detail/list, application form, missing job, modal precedence)")


if __name__ == "__main__":
    asyncio.run(run())
