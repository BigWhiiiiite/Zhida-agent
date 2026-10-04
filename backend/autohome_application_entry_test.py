"""Synthetic entry-only CTA regression; never contacts AutoHome."""
import asyncio
from playwright.async_api import async_playwright
from autohome_navigation_regression_test import BASE, TITLE, card, fixture
from app.ats_adapters import inspect_application_page, start_application


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            await context.route('**/*', lambda r: r.fulfill(body='<html><body></body></html>', content_type='text/html'))
            page = await context.new_page()
            await page.goto(BASE)
            await page.set_content(fixture(card('47900', expanded=True) + card('47898', title='后端开发工程师【2027届校招】')))
            state = await inspect_application_page(page, 'fixture')
            assert state.stage == 'job_detail' and state.job_id == '47900' and state.job_title == TITLE, state.model_dump()
            await page.evaluate("document.addEventListener('click',e=>{if(e.target.closest('.applybtn'))window.clickedPid=e.target.closest('.applybtn').getAttribute('pid')})")
            await start_application(page)
            assert await page.evaluate('[window.applyClicks,window.clickedPid,window.expansionClicks]') == [1, '47900', 0]
            # Multiple expanded jobs cannot choose the first apply CTA.
            await page.set_content(fixture(card('47900', expanded=True) + card('47898', expanded=True)))
            try:
                await start_application(page)
                raise AssertionError('ambiguous entry should be blocked')
            except ValueError:
                pass
            assert await page.evaluate('window.applyClicks') == 0
            await page.goto('https://talent.autohome.com.cn/recruit-delivery.html?pid=47900')
            state = await inspect_application_page(page, 'fixture')
            assert state.stage == 'unknown' and not any(a.intent == 'start_application' for a in state.actions)
            await page.goto('https://account.autohome.com.cn/')
            await page.set_content('''<title>我的汽车之家—登录</title><div class="new-login"><section class="main">
                <ul class="tab"><li class="tab-item tab-item--active" data-area="wechat-code">微信登录</li></ul>
                <section class="wechat-code"><div id="wechat-qr-code"><img id="qrWechatCodeImg" width="100" height="100"></div>
                请使用微信扫码登录</section></section></div>''')
            state = await inspect_application_page(page, 'fixture')
            assert state.stage == 'auth_required' and not state.authenticated
            assert any(a.intent == 'manual_login' for a in state.actions)
            await page.locator('.wechat-code').evaluate("el=>el.innerText='扫描成功，请在手机上确认登录'")
            state = await inspect_application_page(page, 'fixture')
            assert state.stage == 'auth_required' and not state.authenticated
            # A marketing QR code without the scoped login UI is not login evidence.
            await page.set_content('<div class="follow-official-account">请扫码关注汽车之家招聘公众号</div>')
            assert (await inspect_application_page(page, 'fixture')).stage == 'unknown'
        finally:
            await browser.close()
    print('autohome_application_entry_test: OK (selected CTA only, ambiguity denied, delivery is not JD)')


if __name__ == '__main__':
    asyncio.run(run())
