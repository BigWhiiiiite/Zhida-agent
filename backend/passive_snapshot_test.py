"""Anonymous isolated DOM: capture/sync must not click or invalidate login."""
import asyncio
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

from app.browser_service import BrowserDemoService


HTML = '''<meta charset="utf-8"><title>匿名招聘表</title>
<header><label id="account">账号菜单</label>
<div role="combobox" aria-labelledby="account" onclick="logout()">已登录</div></header>
<h2>个人基本信息</h2><form>
<label>姓名<input name="name"></label>
<label>城市<select><option>请选择</option><option>北京</option><option>上海</option></select></label>
<div class="form-item"><label id="ethnicity">民族</label>
<div role="combobox" aria-labelledby="ethnicity" tabindex="0">请选择</div></div>
<div class="form-item"><label id="promise">真实性承诺</label>
<div role="combobox" aria-labelledby="promise" tabindex="0">请选择</div></div>
<div class="form-item"><label>出生日期</label><span class="ant-calendar-picker">
<input readonly placeholder="请选择"></span></div>
</form><script>
window.loggedIn=true;window.clicks=0;window.changes=0;
function logout(){window.loggedIn=false;history.replaceState(null,'','/home')}
document.addEventListener('click',()=>window.clicks++);
document.addEventListener('change',()=>window.changes++);
document.addEventListener('keydown',()=>window.clicks++);
</script>'''


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, channel='chrome')
        try:
            context = await browser.new_context(service_workers='block')
            await context.route('**/*', lambda r: r.fulfill(body=HTML, content_type='text/html'))
            await context.route_web_socket('**/*', lambda ws: ws.close())
            page = await context.new_page()
            await page.goto('https://fixture.example.test/application')
            service = BrowserDemoService()
            service.page, service.session_id = page, 'anonymous'
            preview, dismiss = AsyncMock(), AsyncMock()
            service._preview_field_options, service._dismiss_options = preview, dismiss
            first = await service.snapshot()
            second = await service.snapshot_for('anonymous')
            preview.assert_not_awaited()
            dismiss.assert_not_awaited()
            assert await page.evaluate('()=>[window.loggedIn,window.clicks,window.changes]') == [True,0,0]
            assert page.url.endswith('/application')
            assert not any('账号' in f.question_text for f in second.fields)
            assert next(f for f in first.fields if f.question_text=='城市').options==['北京','上海']
            ethnicity=next(f for f in first.fields if f.question_text=='民族')
            assert not ethnicity.options and ethnicity.options_capture=='deferred'
            assert 'options_deferred' in ethnicity.observation.issues
            birth=next(f for f in first.fields if f.question_text=='出生日期')
            assert birth.control_kind=='calendar' and not birth.date_precision
            assert 'date_precision_missing' in birth.observation.issues
            # Explicit bulk discovery may not click account/declaration fields.
            await service.snapshot(probe_options=True)
            probed=[call.args[0]['question_text'] for call in preview.await_args_list]
            assert probed==['民族','出生日期'], probed
            assert await page.evaluate('()=>[window.loggedIn,window.clicks,window.changes]') == [True,0,0]
        finally:
            await browser.close()
    print('passive_snapshot_test: OK (no clicks/keys/writes/logout, header exclusion, honest deferred options)')


if __name__=='__main__':
    asyncio.run(run())
