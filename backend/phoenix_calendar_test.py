"""Anonymous intercepted calendar fixtures; no live ATS or model request."""
import asyncio
from playwright.async_api import async_playwright
from app.browser_models import ExecutePlanRequest, FillAction
from app.browser_service import BrowserDemoService
from app.models import CandidateProfile, Education
from app.form_agent import create_local_form_plan
from app.phoenix_calendar import canonical_date


def fixture(precision):
    fmt = 'YYYY-MM-DD' if precision == 'date' else 'YYYY-MM'
    return '''<main><h1>你正在投递职位: 匿名智能体工程师（2027校招）(T20000)</h1>
    <section><div>个人信息</div><div class="ux-standard-form"><form class="form">
    <div class="form-item form-item--phoenix"><div class="form-item__title"><label class="form-item__text">最早可实习入职时间</label></div>
    <div class="form-item__control"><div class="phoenix-unmodeled-layer">
    <div class="phoenix-select" onclick="document.querySelector('.phoenix-date-picker').hidden=false"><span class="phoenix-select__placeHolder">请选择日期</span><div class="phoenix-select__content"></div></div>
    <div class="phoenix-date-picker" hidden><input class="phoenix-calendar-input" placeholder="''' + fmt + '''" onkeydown="if(event.key==='Enter'){event.preventDefault();document.querySelector('.phoenix-select__content').textContent=this.value;this.parentElement.hidden=true}"></div>
    </div></div></div><button type="submit" onclick="document.body.dataset.final='yes'">提交申请</button></form></div></section>
    <script>document.addEventListener('click',e=>{if(!e.target.closest('.phoenix-unmodeled-layer'))document.querySelector('.phoenix-date-picker').hidden=true;});</script></main>'''


def month_grid_fixture():
    html = fixture('month')
    start = html.index('<div class="phoenix-date-picker" hidden>')
    end = html.index('</div>',start) + len('</div>')
    months = ''.join('<td class="phoenix-calendar-month-panel-cell"><a class="phoenix-calendar-month-panel-month" onclick="document.querySelector(\'.phoenix-select__content\').textContent=document.querySelector(\'.phoenix-calendar-month-panel-year-select-content\').textContent+\'-' + f'{n:02d}' + '\';this.closest(\'.phoenix-date-picker\').hidden=true">'+str(n)+'月</a></td>' for n in range(1,13))
    menu = '''<div class="phoenix-date-picker" hidden><div class="phoenix-calendar-month-panel">
      <div class="phoenix-calendar-month-panel-header">
      <a class="phoenix-calendar-month-panel-prev-year-btn" onclick="document.querySelector('.phoenix-calendar-month-panel-year-select-content').textContent--">上一年</a>
      <span class="phoenix-calendar-month-panel-year-select-content">2026</span>
      <a class="phoenix-calendar-month-panel-next-year-btn" onclick="document.querySelector('.phoenix-calendar-month-panel-year-select-content').textContent++">下一年</a>
      </div><table class="phoenix-calendar-month-panel-table"><tbody><tr>''' + months + '</tr></tbody></table></div></div>'
    return html[:start]+menu+html[end:]


async def run():
    assert canonical_date('2027-2-1','date') == '2027-02-01'
    assert canonical_date('2021.09','month') == '2021-09'
    for value, precision in [('2027-02','date'), ('2027-02-30','date'), ('2027-13','month')]:
        try:
            canonical_date(value, precision)
        except ValueError:
            pass
        else:
            raise AssertionError('Invalid/imprecise date cannot be guessed')
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome',headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            await context.route('**/*',lambda route:route.fulfill(body=fixture('date'),content_type='text/html'))
            await context.route_web_socket('**/*',lambda socket:socket.close())
            page = await context.new_page()
            await page.goto('https://fixture.zhiye.com/form?jobAdId=anonymous')
            service = BrowserDemoService()
            service.page, service.context, service.session_id = page, context, 'fixture'
            for precision, wanted in [('date','2027-02-01'),('month','2021-09')]:
                await page.set_content(fixture(precision))
                snap = await service.snapshot()
                field = next(f for f in snap.fields if f.label=='最早可实习入职时间')
                assert field.date_precision == precision, field.model_dump()
                assert field.options == [] and field.semantic_key=='preference.available_date'
                result = await service.execute('fixture',ExecutePlanRequest(actions=[FillAction(
                    selector=field.selector,label=field.label,action='select',value=wanted,
                    confidence=1,user_confirmed=True)]))
                assert result.verified==1 and result.failed==0, result.model_dump()
                assert await service._read_field_value(field)==wanted
                assert await page.locator('body').get_attribute('data-final') is None
            # A calendar input in a real form must not implicitly submit it,
            # even if a site omits its own Enter preventDefault handler.
            await page.set_content(fixture('date').replace('event.preventDefault();',''))
            field=next(f for f in (await service.snapshot()).fields if f.label=='最早可实习入职时间')
            await service.execute('fixture',ExecutePlanRequest(actions=[FillAction(selector=field.selector,
                label=field.label,action='select',value='2027-02-01',confidence=1,user_confirmed=True)]))
            assert await page.locator('body').get_attribute('data-final') is None
            await page.set_content(month_grid_fixture())
            field=next(f for f in (await service.snapshot()).fields if f.label=='最早可实习入职时间')
            assert field.date_precision == 'month' and not field.options
            result=await service.execute('fixture',ExecutePlanRequest(actions=[FillAction(selector=field.selector,
                label=field.label,action='select',value='2021-09',confidence=1,user_confirmed=True)]))
            assert result.verified==1 and result.failed==0, result.model_dump()
            assert await service._read_field_value(field)=='2021-09'
            assert await page.locator('body').get_attribute('data-final') is None
            # Coarse resume dates on a day picker require confirmation, not 01.
            field=field.model_copy(update={'semantic_key':'education.start_date','entity_scope':'education:bachelor',
                'label':'开始时间','question_text':'开始时间','section':'教育经历','date_precision':'date'})
            snap=snap.model_copy(update={'fields':[field]})
            plan=create_local_form_plan(snap,CandidateProfile(education=[Education(school='匿名学校',degree='本科',start_date='2021.09')]))
            assert plan.actions[0].action=='ask_user' and not plan.actions[0].value
        finally:
            await browser.close()
    print('phoenix_calendar_test: OK (date/month proof, committed readback, no guessing/submission)')


if __name__=='__main__':
    asyncio.run(run())
