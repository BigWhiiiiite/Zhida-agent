"""Offline browser smoke test for the multi-company application state machine."""

from __future__ import annotations

import asyncio
import os

from playwright.async_api import async_playwright

from app.ats_adapters import (fill_verification_code, inspect_application_page,
                              request_verification_code, start_application)


async def main() -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel=os.getenv("APP_BROWSER_CHANNEL", "chrome"), headless=True
        )
        page = await browser.new_page()

        async def tencent_route(route) -> None:
            if "/login.html" in route.request.url:
                body = """
                <html><head><meta charset="utf-8"><title>腾讯校招登录</title></head><body>
                  <button>QQ账号登录</button><button>微信账号登录</button>
                  <label><input type="checkbox">我已阅读并同意《腾讯招聘隐私政策》</label>
                </body></html>
                """
            else:
                body = """
                <html><head><meta charset="utf-8"><title>Agent开发工程师</title></head><body>
                  <h1>Agent开发工程师</h1>
                  <a href="https://join.qq.com/login.html?state=test">投递简历</a>
                </body></html>
                """
            await route.fulfill(status=200, content_type="text/html", body=body)

        await page.route("**/*", tencent_route)
        await page.goto("https://join.qq.com/post_detail.html?postid=1282707395466077184")
        state = await inspect_application_page(page, "tencent")
        assert state.adapter == "tencent-campus" and state.stage == "job_detail"
        assert state.job_id == "1282707395466077184"
        assert await page.get_by_text("投递简历", exact=True).is_visible()
        await start_application(page)
        state = await inspect_application_page(page, "tencent")
        assert state.stage == "auth_required" and state.requires_consent
        assert {"QQ", "微信"}.issubset(state.authentication_methods)

        async def generic_route(route) -> None:
            await route.fulfill(status=200, content_type="text/html", body="""
            <html><head><meta charset="utf-8"><title>Generic ATS</title></head><body>
              <label>手机号<input name="mobile"></label>
              <button onclick="document.body.dataset.sent='yes'">获取验证码</button>
              <label>验证码<input name="sms_verification" autocomplete="one-time-code"></label>
              <button onclick="document.body.dataset.verified='yes'">验证</button>
              <button onclick="document.body.dataset.final='yes'">提交申请</button>
            </body></html>
            """)

        await page.unroute_all()
        await page.route("**/*", generic_route)
        await page.goto("https://careers.example/application")
        state = await inspect_application_page(page, "generic")
        assert state.stage == "verification_required" and state.final_submit_present
        await request_verification_code(page, "phone", "13800138000")
        assert await page.locator('input[name="mobile"]').input_value() == "13800138000"
        assert await page.locator("body").get_attribute("data-sent") == "yes"
        await fill_verification_code(page, "123456", submit=True)
        assert await page.locator('input[name="sms_verification"]').input_value() == "123456"
        assert await page.locator("body").get_attribute("data-verified") == "yes"
        assert await page.locator("body").get_attribute("data-final") is None

        await page.set_content('<label>图形验证<input name="captcha"></label>')
        state = await inspect_application_page(page, "captcha")
        assert state.stage != "verification_required"
        await browser.close()
    print("Application workflow smoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
