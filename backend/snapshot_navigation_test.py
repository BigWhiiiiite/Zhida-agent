"""A snapshot must not mix a previous form with a replacement document."""
import asyncio
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright
from app import browser_service
from app.browser_service import BrowserDemoService


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel='chrome')
        try:
            page = await browser.new_page()
            await page.route('**/*', lambda r: r.fulfill(body='''<meta charset="utf-8">
                <h2>个人基本信息</h2><label>姓名<input></label>
                <div class="form-item"><label id="city">现居住地</label>
                  <div role="combobox" aria-labelledby="city" tabindex="0">请选择</div></div>''',
                content_type='text/html'))
            service = BrowserDemoService()
            service.page, service.session_id = page, 'anonymous'
            preview = AsyncMock()
            service._preview_field_options = preview
            service._dismiss_options = AsyncMock()
            url = 'https://fixture.example.test/resume'

            async def assert_stopped():
                try:
                    await service.snapshot(probe_options=True)
                    raise AssertionError('accepted old fields on new page')
                except ValueError as exc:
                    assert '旧表单已丢弃' in str(exc)

            # Redirect just after scanning, before options are probed.
            await page.goto(url)
            async def moved_after_scan(*args):
                await page.goto('https://fixture.example.test/home')
            with patch.object(browser_service, 'classify_controls', moved_after_scan):
                await assert_stopped()
            preview.assert_not_awaited()

            # Redirect during a popup probe: do not touch the stale locator
            # again and never publish the old fields as a homepage snapshot.
            await page.goto(url)
            preview.side_effect = moved_after_scan
            await assert_stopped()
            assert preview.await_count == 1
            service._dismiss_options.assert_not_awaited()
            assert await page.locator('input').input_value() == ''
        finally:
            await browser.close()
    print('snapshot_navigation_test: OK (redirect discards old fields, no stale probes or writes)')


if __name__ == '__main__':
    asyncio.run(run())
