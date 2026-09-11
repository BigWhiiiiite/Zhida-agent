"""Offline browser smoke test for the multi-company application state machine."""

from __future__ import annotations

import asyncio
import os

from playwright.async_api import async_playwright

from app.ats_adapters import (continue_application, create_account, fill_registration_info,
                              fill_verification_code, inspect_application_page,
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

        await page.set_content("""
        <main>
          <h1>Create account</h1>
          <label>Email<input type="email" name="email" required></label>
          <label>Phone<input type="tel" name="mobile"></label>
          <label>Password<input type="password" name="password" required></label>
          <label>Confirm password<input type="password" name="confirm_password" required></label>
          <label><input type="checkbox" required>I agree to the privacy terms</label>
          <button onclick="document.body.dataset.created='yes'">Create account</button>
        </main>
        """)
        state = await inspect_application_page(page, "register")
        assert state.stage == "registration_required" and state.requires_consent
        assert set(state.registration_identifiers) == {"email", "phone"}
        await fill_registration_info(page, "candidate@example.com", "13800138000", "SafePass123!")
        assert await page.locator('input[name="email"]').input_value() == "candidate@example.com"
        assert await page.locator('input[name="confirm_password"]').input_value() == "SafePass123!"
        try:
            await create_account(page)
            raise AssertionError("unchecked consent must block account creation")
        except ValueError as exc:
            assert "隐私协议" in str(exc)
        await page.locator('input[type="checkbox"]').check()
        await create_account(page)
        assert await page.locator("body").get_attribute("data-created") == "yes"

        await page.set_content("""
        <main><h1>Create account</h1>
          <label>Email<input type="email" name="email" required></label>
          <button onclick="document.body.dataset.created='magic-link'">Create account</button>
        </main>
        """)
        state = await inspect_application_page(page, "passwordless-register")
        assert state.stage == "registration_required" and not state.registration_requires_password
        await fill_registration_info(page, "candidate@example.com", "", "")
        await create_account(page)
        assert await page.locator("body").get_attribute("data-created") == "magic-link"

        await page.set_content("""
        <main><p>Step 1 of 2</p>
          <label>Name<input name="name" required value="李春博"></label>
          <button onclick="document.body.dataset.next='yes'">Save and continue</button>
        </main>
        """)
        state = await inspect_application_page(page, "steps")
        assert state.stage == "application_form" and state.safe_next_present
        assert (state.page_step_current, state.page_step_total) == (1, 2)
        await continue_application(page)
        assert await page.locator("body").get_attribute("data-next") == "yes"

        await page.set_content('<button onclick="document.body.dataset.final=\'yes\'">提交申请</button>')
        state = await inspect_application_page(page, "final")
        assert state.stage == "review" and state.final_submit_present and not state.safe_next_present
        try:
            await continue_application(page)
            raise AssertionError("final submit must never be clicked")
        except ValueError as exc:
            assert "最终提交" in str(exc)
        assert await page.locator("body").get_attribute("data-final") is None
        await browser.close()
    print("Application workflow smoke test passed")


if __name__ == "__main__":
    asyncio.run(main())
