"""Full snapshot regression against anonymous captured form structure."""
import asyncio
from pathlib import Path
from playwright.async_api import async_playwright
from app.browser_service import BrowserDemoService
from app.ats_adapters import inspect_application_page


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            await context.route('**/*', lambda r: r.fulfill(body=Path('fixtures/autohome_delivery_form.html').read_text(), content_type='text/html'))
            page = await context.new_page()
            await page.goto('https://talent.autohome.com.cn/recruit-delivery.html?pid=47900')
            service = BrowserDemoService()
            service.page, service.context, service.session_id = page, context, 'fixture'
            snapshot = await service.snapshot()
            assert len(snapshot.fields) == 13, [(f.label, f.field_type) for f in snapshot.fields]
            assert len([f for f in snapshot.fields if f.field_type == 'combobox']) == 5
            dates = [f for f in snapshot.fields if f.label in {'开始时间', '结束时间'}]
            assert len(dates) == 4 and all(f.required for f in dates)
            assert len({f.container_key for f in dates}) == 2
            assert {f.entity_scope for f in dates} == {'education:bachelor', 'education:unspecified'}
            assert all('汽车之家 招聘职位' not in f.label for f in snapshot.fields)
            assert any('我承诺' in f.label and f.required for f in snapshot.fields)
            state = await inspect_application_page(page, 'fixture')
            assert state.final_submit_present and state.stage == 'review'
            check = await service.pre_submit_check('fixture')
            assert not check.ready and check.required_total == 13
            assert len(check.required_missing) == 13
            assert check.submit_labels == ['提交简历']
        finally:
            await browser.close()
    print('autohome_snapshot_test: OK (13 logical fields; labels, required, education isolation, submit guard)')


if __name__ == '__main__':
    asyncio.run(run())
