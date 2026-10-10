"""Anonymous, network-disabled old/new Ant fixtures; no user/browser session."""
import asyncio
import json
import re
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

from app.ats_controls import scoped_option_entries, visible_popup_ids
from app.ats_registry import policy_for
from app.browser_service import BrowserDemoService, _best_option
from app.application_models import ApplicationWorkflowState
from app.browser_models import ExecutePlanRequest, FillAction
from app.structured_controls import classify_controls, preview_calendar, select_ant_calendar, select_ant_cascade
from app.safari_browser import SafariContext, CREATE_WINDOW


HTML = r'''<meta charset="utf-8"><style>.ant-select-selection{min-height:30px;min-width:120px}</style><form id="form">
<div class="ant-form-item"><label>出生日期</label><span id="birth" class="ant-calendar-picker"><input class="ant-calendar-picker-input" placeholder="请选择" readonly></span></div>
<div class="ant-form-item"><label>现居住地</label><span id="area" class="ant-cascader-picker"><input readonly></span></div>
<div class="ant-form-item"><label>硕士学历</label><div id="degree" class="ant-select-selection" role="combobox"><div class="ant-select-selection-selected-value"></div></div></div>
<div class="ant-form-item"><label>备注</label><input id="notes"></div>
<button type="submit">提交申请</button></form>
<div id="cal" class="ant-calendar" style="display:none"><input class="ant-calendar-input" placeholder="YYYY-MM-DD"><div class="ant-calendar-date-panel"><table class="ant-calendar-table"></table></div></div>
<div id="cascade" class="ant-cascader-menus" style="display:none"><ul class="ant-cascader-menu"><li class="ant-cascader-menu-item ant-cascader-menu-item-expand">北京市</li><li class="ant-cascader-menu-item ant-cascader-menu-item-disabled">其他</li></ul></div>
<div id="choices" class="ant-select-dropdown" style="display:none"><ul><li class="ant-select-dropdown-menu-item">本科</li><li class="ant-select-dropdown-menu-item">硕士研究生</li><li class="ant-select-dropdown-menu-item ant-select-dropdown-menu-item-disabled">博士</li></ul></div>
<script>
window.submits=0;form.addEventListener('submit',e=>{e.preventDefault();window.submits++});
birth.onclick=()=>cal.style.display='block';
area.onclick=()=>cascade.style.display='block';
degree.onclick=()=>choices.style.display='block';
document.addEventListener('keydown',e=>{if(e.keyCode===27)for(const p of [cal,cascade,choices])p.style.display='none'});
cal.querySelector('input').addEventListener('keydown',e=>{if(e.keyCode===13){birth.querySelector('input').value=e.target.value;cal.style.display='none'}});
choices.querySelectorAll('li').forEach(li=>li.onclick=()=>{degree.firstElementChild.textContent=li.textContent;choices.style.display='none'});
cascade.querySelector('li').onclick=()=>{
 if(cascade.children.length>1)cascade.children[1].remove();
 const col=document.createElement('ul');col.className='ant-cascader-menu';
 col.innerHTML='<li class="ant-cascader-menu-item">海淀区</li><li class="ant-cascader-menu-item">朝阳区</li>';
 col.querySelectorAll('li').forEach(li=>li.onclick=()=>{area.querySelector('input').value='北京市 / '+li.textContent;cascade.style.display='none'});
 cascade.appendChild(col);
};
</script>'''

# Modern outer/control class shape was observed through Zhida's live, read-only
# diagnostic UI. Anonymous behaviour fixture is NOT a live-bank success claim.
MODERN = r'''<meta charset="utf-8"><form id="form">
<div class="ant-form-item"><label>出生日期</label><div id="modern" class="ant-picker"><div class="ant-picker-input"><input placeholder="请选择"></div></div></div>
<button type="submit">提交申请</button></form>
<div id="modernPanel" class="ant-picker-dropdown" style="display:none"><div class="ant-picker-panel"><div class="ant-picker-date-panel"><table><tbody><tr><td class="ant-picker-cell" title="2001-02-03">3</td></tr></tbody></table></div></div></div>
<script>
window.submits=0;window.committed='';form.addEventListener('submit',e=>{e.preventDefault();window.submits++});
modern.onclick=()=>modernPanel.style.display='block';
document.addEventListener('keydown',e=>{if(e.keyCode===27){modernPanel.style.display='none';modern.querySelector('input').value=window.committed}});
modern.querySelector('input').addEventListener('keydown',e=>{
 if(e.keyCode===13){
  if(e.target.value==='2001-02-03'){window.committed=e.target.value;modernPanel.querySelector('td').classList.add('ant-picker-cell-selected')}
  modernPanel.style.display='none';
 }
});
</script>'''

PORTALS = r'''<meta charset="utf-8">
<div id="degree" class="ant-select"><div class="ant-select-selector"><input role="combobox" aria-controls="owned-list"></div></div>
<div id="owned" class="ant-select-dropdown" style="display:none">
 <div id="owned-list" role="listbox" style="height:0;width:0;overflow:hidden"><div role="option">code-1</div></div>
 <div class="ant-select-item-option">本科</div><div class="ant-select-item-option">硕士研究生</div>
</div>
<div class="ant-select-dropdown"><div class="ant-select-item-option">邻题选项</div></div>
<div id="area" class="ant-select ant-cascader"><div class="ant-select-selector"><input role="combobox" aria-controls="area-list"></div></div>
<div id="areaPanel" class="ant-select-dropdown ant-cascader-dropdown" style="display:none">
 <div id="area-list" role="listbox" style="height:0;width:0;overflow:hidden"></div>
 <div class="ant-cascader-menus"><ul class="ant-cascader-menu"><li class="ant-cascader-menu-item ant-cascader-menu-item-expand">北京市</li></ul></div>
</div>
<script>
degree.onclick=()=>owned.style.display='block';area.querySelector('.ant-select-selector').onclick=()=>areaPanel.style.display='block';
document.addEventListener('keydown',e=>{if(e.keyCode===27){owned.style.display='none';areaPanel.style.display='none'}});
areaPanel.querySelector('li').onclick=()=>{
 const col=document.createElement('ul');col.className='ant-cascader-menu';col.innerHTML='<li class="ant-cascader-menu-item">海淀区</li>';
 col.querySelector('li').onclick=()=>{area.querySelector('input').value='北京市 / 海淀区';areaPanel.style.display='none'};
 areaPanel.querySelector('.ant-cascader-menus').appendChild(col);
};
</script>'''


async def portal_tests(page, transport):
    await page.set_content(PORTALS)
    policy=policy_for('generic');degree=transport.locator('#degree')
    await degree.click()
    entries=await scoped_option_entries(transport,degree,policy)
    assert [label for label,_ in entries]==['本科','硕士研究生'],[label for label,_ in entries]
    await degree.locator('input').evaluate("el=>el.setAttribute('aria-controls','missing')")
    assert not await scoped_option_entries(transport,degree,policy)
    assert await select_ant_cascade(transport,transport.locator('#area'),'北京市 / 海淀区',_best_option)==['北京市 / 海淀区']


async def modern_tests(page, transport):
    await page.set_content(MODERN)
    control=transport.locator('#modern')
    assert await preview_calendar(transport,control)=='date'
    assert await control.locator('input').input_value()==''
    assert await select_ant_calendar(transport,control,'2001-02-03','date')=='2001-02-03'
    assert await control.locator('input').input_value()=='2001-02-03'
    assert await page.evaluate('()=>window.submits')==0
    # A syntactically valid date rejected by the widget must not count as a
    # success just because the input temporarily contains the target string.
    try:
        await select_ant_calendar(transport,control,'2001-02-04','date')
        raise AssertionError('typed but uncommitted date accepted')
    except ValueError as e:
        assert '选中状态' in str(e)
    assert await control.locator('input').input_value()=='2001-02-03'
    await control.locator('input').evaluate('el=>el.readOnly=true')
    try:
        await select_ant_calendar(transport,control,'2001-02-04','date')
        raise AssertionError('readonly modern picker overwritten')
    except ValueError as e:
        assert '不会强写' in str(e)
    assert await page.evaluate('()=>window.submits')==0


async def readonly_calendar_tests(page, transport):
    await page.set_content(MODERN)
    await page.locator('#modern input').evaluate('el=>el.readOnly=true')
    await page.locator('#modernPanel td').evaluate(r'''cell => {
      cell.innerHTML='<div class="ant-picker-cell-inner">3</div>';
      cell.firstChild.onclick=()=>{
        window.committed=cell.title;
        document.querySelector('#modern input').value=cell.title;
        cell.classList.add('ant-picker-cell-selected');
        modernPanel.style.display='none';
      };
    }''')
    # A neighbouring question's duplicate cell must never be used.
    await page.evaluate(r'''() => {
      const other=document.createElement('div');
      other.className='ant-picker-dropdown';
      other.innerHTML='<td class="ant-picker-cell" title="2001-02-03">3</td>';
      document.body.appendChild(other);
    }''')
    control=transport.locator('#modern')
    assert await select_ant_calendar(transport,control,'2001-02-03','date')=='2001-02-03'
    assert await control.locator('input').input_value()=='2001-02-03'
    assert await control.locator('input').evaluate('el=>el.readOnly')
    await page.locator('#modernPanel td').evaluate("el=>el.classList.add('ant-picker-cell-disabled')")
    try:
        await select_ant_calendar(transport,control,'2001-02-03','date')
        raise AssertionError('disabled calendar cell selected')
    except ValueError as exc:
        assert '不会强写' in str(exc)
    assert await page.evaluate('()=>window.submits')==0


async def tests(page):
    await page.set_content(HTML)
    fields=[{'selector':'#birth','field_type':'combobox'},{'selector':'#area','field_type':'combobox'},
            {'selector':'#degree','field_type':'combobox'},{'selector':'#notes','field_type':'text'}]
    await classify_controls(page,fields)
    assert [f['control_kind'] for f in fields]==['calendar','cascade','dropdown','text']
    birth=page.locator('#birth')
    assert await preview_calendar(page,birth)=='date'
    assert not await page.locator('#cal').is_visible()
    try:
        await select_ant_calendar(page,birth,'2025-09','date')
        raise AssertionError('invented missing day')
    except ValueError as e:
        assert '每月1日' in str(e)
    assert await birth.locator('input').input_value()==''
    assert await select_ant_calendar(page,birth,'2001-02-03','date')=='2001-02-03'
    assert await birth.locator('input').input_value()=='2001-02-03'
    assert await page.evaluate('()=>window.submits')==0

    area=page.locator('#area')
    try:
        await select_ant_cascade(page,area,'北京',_best_option)
        raise AssertionError('invented unspecified district')
    except ValueError as e:
        assert '下级' in str(e)
    assert await area.locator('input').input_value()==''
    assert await select_ant_cascade(page,area,'北京市 / 海淀区',_best_option)==['北京市 / 海淀区']
    assert await area.locator('input').input_value()=='北京市 / 海淀区'

    degree=page.locator('#degree');policy=policy_for('generic')
    before=await visible_popup_ids(page,policy)
    await degree.click()
    entries=await scoped_option_entries(page,degree,policy,before)
    assert [label for label,_ in entries]==['本科','硕士研究生']
    await next(option for label,option in entries if label=='硕士研究生').click()
    assert await degree.inner_text()=='硕士研究生'
    # A broken ARIA link must not borrow an unrelated visible popup.
    await degree.evaluate("el=>el.setAttribute('aria-controls','missing')")
    await degree.click()
    assert not await scoped_option_entries(page,degree,policy,before)
    await degree.press('Escape')
    assert not await page.locator('#choices').is_visible()
    assert await page.evaluate('()=>window.submits')==0


async def run():
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,channel='chrome')
        page=await browser.new_page()
        page.set_default_timeout(5000)
        await page.route('**/*',lambda route:route.abort())
        await tests(page)
        # Exercise SAME Safari-generated scripts through an isolated fixture,
        # without Apple Events, Safari windows, accounts or external navigation.
        async def runner(script,*args):
            if script==CREATE_WINDOW:
                return '81|1|'+args[0]
            if 'close window id' in script:
                return ''
            if 'return URL of tab tabIndex' in script:
                return page.url
            match=re.search(r'return do JavaScript (".*") in tab tabIndex',script)
            assert match,'Unexpected transport command'
            return await page.evaluate(json.loads(match[1]))
        context=SafariContext(runner)
        native=await context.new_page()
        # rc-select keeps a portalled menu alive only while its search input
        # retains focus. The transport must not blur that input after typing.
        await page.set_content('''<input id="search" role="combobox"><div id="menu">选项</div>
          <script>search.onblur=()=>menu.hidden=true;</script>''')
        await native.locator('#search').fill('匿名搜索', keep_focus=True)
        assert await page.locator('#menu').is_visible()
        assert await page.evaluate('document.activeElement.id')=='search'
        await native.locator('#search').fill('匿名普通输入')
        assert not await page.locator('#menu').is_visible()
        # A privacy-rounded/jittering timeOrigin is not a document reload.
        handle=await native.evaluate_handle('()=>document')
        await page.evaluate("Object.defineProperty(performance,'timeOrigin',{get:()=>Math.random()})")
        assert await handle.evaluate('original=>original===document')
        await page.set_content('<p>替换文档</p>')
        # set_content replaces contents, not the Document; a real navigation
        # reload is covered separately by execute_page_transition_test.
        assert await handle.evaluate('original=>original===document')
        await handle.dispose()
        await portal_tests(page,page)
        await portal_tests(page,native)
        await modern_tests(page,page)
        await modern_tests(page,native)
        await readonly_calendar_tests(page,page)
        await readonly_calendar_tests(page,native)
        await page.set_content(HTML)
        fields=[{'selector':'#birth','field_type':'combobox'},{'selector':'#area','field_type':'combobox'}]
        await classify_controls(native,fields)
        assert fields[0]['control_kind']=='calendar'
        assert await preview_calendar(native,native.locator('#birth'))=='date'
        assert await select_ant_calendar(native,native.locator('#birth'),'2001-02-03','date')=='2001-02-03'
        assert await native.locator('#birth input').input_value()=='2001-02-03'
        assert await select_ant_cascade(native,native.locator('#area'),'北京市 / 海淀区',_best_option)==['北京市 / 海淀区']
        assert await native.locator('#area input').input_value()=='北京市 / 海淀区'
        assert await page.evaluate('()=>window.submits')==0
        # Full scanner recognises canonical wrappers, not extra panel inputs.
        service=BrowserDemoService();service.page=page;service.session_id='fixture'
        scanned=await service.snapshot()
        kinds={f.question_text:f.control_kind for f in scanned.fields}
        assert kinds['出生日期']=='calendar' and kinds['现居住地']=='cascade',kinds
        assert all('YYYY' not in f.question_text for f in scanned.fields)
        # Exercise the real executor, including its outer-control readback,
        # rather than only proving that a widget accepted an input event.
        values={'出生日期':'2001-02-03','现居住地':'北京市 / 海淀区','硕士学历':'硕士研究生','备注':'匿名测试内容'}
        for transport in (page,native):
            await page.set_content(HTML)
            service.page=transport
            service.context=context if transport is native else None
            service.workflow_state=AsyncMock(side_effect=lambda session: ApplicationWorkflowState(
                session_id=session,url=transport.url,title='Anonymous',stage='application_form'))
            scanned=await service.snapshot()
            actions=[FillAction(selector=f.selector,label=f.question_text,
                action='fill' if f.field_type=='text' else 'select',value=values[f.question_text],
                confidence=1,user_confirmed=True) for f in scanned.fields if f.question_text in values]
            result=await service.execute('fixture',ExecutePlanRequest(actions=actions))
            assert result.verified==4 and result.failed==0,[r.model_dump() for r in result.results]
            assert await page.evaluate('()=>window.submits')==0
            await page.set_content(MODERN)
            scanned=await service.snapshot()
            date=next(f for f in scanned.fields if f.question_text=='出生日期')
            assert date.control_kind=='calendar' and date.date_precision==''
            result=await service.execute('fixture',ExecutePlanRequest(actions=[FillAction(
                selector=date.selector,label=date.question_text,action='select',value='2001-02-03',
                confidence=1,user_confirmed=True)]))
            assert result.verified==1 and result.failed==0,[r.model_dump() for r in result.results]
            assert await page.evaluate('()=>window.submits')==0
        await browser.close()
    print('structured_controls_test: OK (text/dropdown/calendar/cascade, exact scope, precision, native Safari scripts, no submission)')


if __name__=='__main__':asyncio.run(run())
