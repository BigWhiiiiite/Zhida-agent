"""Intercepted anonymous area picker regressions; no live site connections."""
import asyncio

from playwright.async_api import async_playwright

from app.browser_models import ExecutePlanRequest, FillAction
from app.browser_service import BrowserDemoService
from app.form_agent import build_form_review, create_local_form_plan
from app.models import CandidateProfile
from app.region_facts import region_values_match
from phoenix_control_test import question, record


HTML = '''<style>.phoenix-select{width:250px;height:32px;border:1px solid gray}.area-item-name{height:28px}.icon-container,.area-text-label,.phoenix-button{display:inline-block;min-width:20px;min-height:20px}</style><header><span>+86 100****0000</span></header><h1>你正在投递职位: 匿名智能体工程师（2027校招）(T10000)</h1>''' + record('个人信息',
    question('籍贯', '''<div id="region" class="phoenix-select" onclick="openMenu(this)"><ul class="phoenix-select__content"></ul></div>''', True) +
    question('现居住地', '''<div id="currentRegion" class="phoenix-select" onclick="openMenu(this)"><ul class="phoenix-select__content"></ul></div>''')) + '''
<div id="picker" hidden>
  <div class="left-container"><div><div class="area-tab-list"><div class="area-tab-item active">地区</div></div>
    <div class="area-breadcrumb-list"><div class="phoenix-breadcrumb" id="crumbs"></div></div></div>
    <div class="area-data-container" id="options"></div></div>
  <div class="footer"><div class="phoenix-button" onclick="commitRegion()"><div class="phoenix-button__content">确定</div></div>
    <div class="phoenix-button" onclick="picker.hidden=true"><div class="phoenix-button__content">取消</div></div></div>
</div>
<div class="phoenix-button" onclick="document.body.dataset.decoy='clicked'">确定</div>
<button type="submit" onclick="document.body.dataset.final='clicked'">提交申请</button>
<script>
const picker=document.getElementById('picker');let trail=[],selected=[],committed=[],activeRegion=null;
const tree={'': ['河北省','北京市'], '河北省':['保定市'], '河北省/保定市':['莲池区','竞秀区']};
function openMenu(el){activeRegion=el;trail=[];selected=[];picker.hidden=false;render();}
function render(){
 document.getElementById('crumbs').innerHTML=['全国',...trail].map((s,i)=>`<span class="phoenix-breadcrumb-text" onclick="event.stopPropagation();trail=trail.slice(0,${i});render()">${s}</span>`).join('');
 document.getElementById('options').innerHTML=(tree[trail.join('/')]||[]).map((s,i)=>`<div class="area-item-container"><div class="area-item-name">
 <span class="icon-container visible" onclick="event.stopPropagation();selected=[...trail,'${s}'];render()"><svg width="20" height="20" class="area-icon-${selected.join('/')==[...trail,s].join('/')?'RadioChecked':'RadioUnchecked'}"></svg></span>
 <span class="area-text-label" onclick="event.stopPropagation();trail.push('${s}');render()">${s}</span>${tree[[...trail,s].join('/')]?'<svg width="20" height="20" class="area-icon-right visible"></svg>':''}</div></div>`).join('');
}
function commitRegion(){committed=[...selected];activeRegion.querySelector('.phoenix-select__content').innerHTML='<li>'+committed[committed.length-1]+'</li>';picker.hidden=true;document.body.dataset.commits=String(Number(document.body.dataset.commits||0)+1);}
document.addEventListener('click',e=>{if(!e.target.closest('#picker,.phoenix-select'))picker.hidden=true;});
</script>'''


async def run():
    assert region_values_match('河北省，保定市，莲池区', '河北省/保定市/莲池区')
    assert not region_values_match('河北省保定市莲池区', '河北省/保定市')
    assert not region_values_match('河北省保定市莲池区', '莲池区')
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        try:
            ctx = await browser.new_context(service_workers='block')
            await ctx.route('**/*', lambda route: route.fulfill(body=HTML, content_type='text/html; charset=utf-8'))
            await ctx.route_web_socket('**/*', lambda socket: socket.close())
            page = await ctx.new_page()
            await page.goto('https://fixture.zhiye.com/form?jobAdId=anonymous')
            svc = BrowserDemoService()
            svc.page, svc.context, svc.session_id = page, ctx, 'fixture'
            snap = await svc.snapshot()
            field = next(f for f in snap.fields if f.label == '籍贯')
            assert field.region_picker and field.options == ['河北省', '北京市'], field.model_dump()
            profile = CandidateProfile(hometown='河北省，保定市，莲池区')
            plan = create_local_form_plan(snap, profile)
            action = next(a for a in plan.actions if a.selector == field.selector)
            assert action.action == 'select' and not action.needs_model, action.model_dump()
            result = await svc.execute('fixture', ExecutePlanRequest(actions=[action]))
            assert result.verified == 1 and result.failed == 0, result.model_dump()
            assert await page.locator('body').get_attribute('data-commits') == '1'
            fresh = await svc.snapshot()
            assert next(f for f in fresh.fields if f.label=='籍贯').region_value_path == '河北省/保定市/莲池区'
            review = build_form_review(fresh, create_local_form_plan(fresh, profile))
            assert review.summary.matched == 1, review.model_dump()
            # Multiple controls share one portal. Filling residence must not
            # invalidate a different control's verified hometown.
            residence = next(f for f in fresh.fields if f.label=='现居住地')
            result = await svc.execute('fixture', ExecutePlanRequest(actions=[FillAction(
                selector=residence.selector,label='现居住地',action='select',value='北京市',confidence=1)]))
            assert result.verified == 1, result.model_dump()
            assert await svc._read_field_value(field) == '河北省/保定市/莲池区'
            # A user opening/editing the widget invalidates the write audit.
            await page.locator('#region').click()
            assert await svc._read_field_value(field) == '莲池区'
            # Reopening retains the city list. Reset through owned 全国 then
            # commit municipality; never click another dialog's 确定.
            field = next(f for f in fresh.fields if f.label == '籍贯')
            result = await svc.execute('fixture', ExecutePlanRequest(actions=[FillAction(
                selector=field.selector, label='籍贯', action='select', value='北京', confidence=1)]))
            assert result.verified == 1, result.model_dump()
            assert await svc._read_field_value(field) == '北京市'
            before = await svc._read_field_value(field)
            result = await svc.execute('fixture', ExecutePlanRequest(actions=[FillAction(
                selector=field.selector, label='籍贯', action='select', value='河北省保定市不存在区', confidence=1)]))
            assert result.failed == 1 and await svc._read_field_value(field) == before
            assert await page.locator('body').get_attribute('data-decoy') is None
            assert await page.locator('body').get_attribute('data-final') is None
        finally:
            await browser.close()
    print('phoenix_region_test: OK (owned levels, radio+confirm, re-read, no guessed parents or global confirmation)')


if __name__ == '__main__':
    asyncio.run(run())
