"""Bounded Safari menu preview on anonymous local DOM, never live Safari."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from app import browser_service
from app.browser_service import BrowserDemoService
from app.safari_browser import SafariContext


class LocalPage:
    def __init__(self, page):
        self.page = page
        self.refresh = AsyncMock()

    def __getattr__(self, key):
        return getattr(self.page, key)


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel='chrome')
        try:
            page = await browser.new_page()
            choices = ''.join(f'<div class="form-item"><label id="q{i}">匿名选择题{i}</label>'
                f'<div role="combobox" tabindex="0" aria-labelledby="q{i}">请选择</div></div>'
                for i in range(20))
            html = ('<meta charset="utf-8"><h2>个人基本信息</h2>'
                    '<label>姓名<input name="candidate"></label>'
                    '<label>城市<select><option>请选择</option><option>北京</option></select></label>' + choices)
            await page.route('**/*', lambda r: r.fulfill(body=html, content_type='text/html'))
            await page.goto('https://fixture.example.test/application')
            service = BrowserDemoService()
            service.page, service.context, service.session_id = LocalPage(page), SafariContext(), 'fixture'
            clock = SimpleNamespace(now=0.0)
            clock.monotonic = lambda: clock.now
            probed = []

            async def expensive_preview(item, locator, policy):
                probed.append(item['label'])
                item['options'] = ['选项一', '选项二']
                clock.now += 11  # synthetic IPC cost, no wall-clock sleep

            service._preview_field_options = expensive_preview
            service._dismiss_options = AsyncMock()
            with patch.object(browser_service, 'time', clock):
                sample = await service.snapshot(probe_options=True)
            assert len(probed) == 1, probed
            assert len(sample.fields) == 22
            assert any(f.label == '姓名' for f in sample.fields)
            assert next(f for f in sample.fields if f.label == '城市').options == ['北京']
            menus = [f for f in sample.fields if f.field_type == 'combobox']
            assert len(menus) == 20 and menus[0].options == ['选项一', '选项二']
            assert all(not f.options and '尚未读取完成' in f.help_text for f in menus[1:])
            assert service._dismiss_options.await_count == 1
            assert await page.locator('input').input_value() == ''

            # A slow single menu is deferred, and its popup is dismissed.
            clock.now = 0
            async def timeout_preview(item, locator, policy):
                item['options'] = ['不能保留的半截选项']
                clock.now += 11
                raise asyncio.TimeoutError()
            service._preview_field_options = timeout_preview
            with patch.object(browser_service, 'time', clock):
                sample = await service.snapshot(probe_options=True)
            assert all(not f.options for f in sample.fields if f.field_type == 'combobox')
            assert service._dismiss_options.await_count == 2

            # A popup that cannot be safely closed remains a hard stop.
            clock.now = 0
            service._dismiss_options = AsyncMock(side_effect=asyncio.TimeoutError())
            with patch.object(browser_service, 'time', clock):
                try:
                    await service.snapshot(probe_options=True)
                    raise AssertionError('Unclosed popup accepted')
                except ValueError as exc:
                    assert '关闭网页选项预览超时' in str(exc)
            assert await page.locator('input').input_value() == ''
        finally:
            await browser.close()
    print('safari_option_budget_test: OK (bounded preview, intact text/native choices, explicit deferral, cleanup guard, no writes)')


if __name__ == '__main__':
    asyncio.run(run())
