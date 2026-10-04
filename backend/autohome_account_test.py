"""Isolated Chromium account-evidence regressions; every request is intercepted."""
from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from app.autohome_entry import inspect_autohome_account


BASE = "https://talent.autohome.com.cn/campus-recruit-list.html?targetId=1004"
NAME = '<a class="name" href="personal-center.html">测试车友</a>'
LOGOUT = '<div class="dropdown-content logout" style="display:none"><a class="dc" onclick="logout()">退出</a></div>'
ACCOUNT = '<div class="dropdown">' + NAME + LOGOUT + '</div>'


def fixture(content: str, header_attributes: str = "") -> str:
    return f'''<!doctype html><html><body><header id="header" {header_attributes}>
      <div class="login-box-n">{content}</div></header>
      <script>window.clicks=0;document.addEventListener('click',()=>window.clicks++);</script></body></html>'''


async def run() -> None:
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

            async def check(html: str, expected: bool) -> None:
                await page.set_content(html)
                before = await page.content()
                assert await inspect_autohome_account(page) is expected, html
                assert await page.content() == before, "inspection changed the DOM"
                assert await page.evaluate("window.clicks") == 0

            await check(fixture(ACCOUNT), True)
            await check(fixture(ACCOUNT.replace('href="personal-center.html"',
                'href="https://talent.autohome.com.cn/personal-center.html"')), True)
            await check(fixture(ACCOUNT.replace('style="display:none"', 'style="display:block"')), True)
            await check(fixture(ACCOUNT).replace('class="login-box-n"', 'class="login-box-n" style="display:contents"'), True)

            for style in ('display:none', 'visibility:hidden', 'opacity:0'):
                await check(fixture(ACCOUNT.replace('class="name"', f'class="name" style="{style}"')), False)
                await check(fixture(ACCOUNT, f'style="{style}"'), False)
            await check(fixture(ACCOUNT.replace('class="name"', 'class="name" hidden')), False)
            await check(fixture(ACCOUNT, 'aria-hidden="true"'), False)
            await check(fixture(ACCOUNT.replace('测试车友', '   ')), False)
            await check(fixture(ACCOUNT.replace('测试车友', '登录')), False)
            await check(fixture(ACCOUNT.replace(LOGOUT, '')), False)
            await check(fixture(ACCOUNT.replace('logout()', 'otherFunction()')), False)
            await check(fixture(ACCOUNT.replace('退出', '退出申请')), False)
            await check(fixture('<div class="dropdown">' + NAME + '</div><div class="dropdown">' + LOGOUT + '</div>'), False)
            await check(fixture(ACCOUNT + ACCOUNT), False)
            await check(fixture(ACCOUNT + '<a class="btn" onclick="login()">登录</a>'), False)
            await check(fixture('<a class="btn" onclick="login()">登录</a>'), False)
            for href in ('https://example.test/personal-center.html', 'javascript:logout()',
                         'https://other@talent.autohome.com.cn/personal-center.html', '/jobs',
                         '/personal-center.html?other=1', 'https://%'):
                await check(fixture(ACCOUNT.replace('href="personal-center.html"', f'href="{href}"')), False)
            await check(fixture(ACCOUNT).replace('id="header"', 'id="page-content"'), False)
            for url in ('https://account.autohome.com.cn/', 'https://talent.autohome.com.cn.example.test/',
                        'https://example.test/', 'http://talent.autohome.com.cn/'):
                await page.goto(url)
                await check(fixture(ACCOUNT), False)
            assert all(method == "GET" for method, _ in requests), requests
        finally:
            await browser.close()
    print("autohome_account_test: OK (official host, visible nickname, hidden logout, same dropdown, no interactions)")


if __name__ == "__main__":
    asyncio.run(run())
