"""Anonymous, disconnected preview fixtures: open only, never select or fill.

Uses a fresh headless Chromium page, not Safari, a real site or a user session.
Every network/WebSocket request is blocked; no database/model imports or calls.
"""
import asyncio
import json

from playwright.async_api import async_playwright

from app.structured_controls import classify_controls, preview_calendar, preview_cascade


CASCADE = r'''<meta charset="utf-8"><style>
 .ant-cascader {display:block;width:250px;height:35px}
 .ant-cascader-menu {display:inline-block;vertical-align:top;width:160px}
 .ant-cascader-menu-item {display:block;min-height:25px}
</style><form id="fixture-form">
 <div id="area" class="ant-cascader"><input readonly value="保留的已选路径" aria-controls="owned-list"></div>
 <button type="submit">提交</button>
</form>
<div id="neighbour" class="ant-cascader-dropdown"><ul id="neighbour-list" class="ant-cascader-menu">
 <li class="ant-cascader-menu-item">邻题选项</li></ul></div>
<div id="owned" class="ant-cascader-dropdown" style="display:none">
 <div id="owned-list" role="listbox" style="height:0;width:0;overflow:hidden"></div>
 <div class="ant-cascader-menus">
  <ul class="ant-cascader-menu" aria-level="1">
   <li class="ant-cascader-menu-item ant-cascader-menu-item-expand">北京市<span style="display:none">隐藏说明</span><svg><title>箭头</title></svg></li>
   <li class="ant-cascader-menu-item ant-cascader-menu-item-disabled">其他</li>
   <li class="ant-cascader-menu-item" hidden>隐藏选项</li>
   <li class="ant-cascader-menu-item" style="display:none">CSS隐藏选项</li>
  </ul>
  <ul class="ant-cascader-menu" aria-level="2"><li class="ant-cascader-menu-item">海淀区</li></ul>
  <div style="visibility:hidden"><ul class="ant-cascader-menu"><li class="ant-cascader-menu-item">隐藏层</li></ul></div>
 </div>
</div>
<script>
window.events={input:0,change:0,submit:0,open:0,option:0};
for(const name of ['input','change','submit'])document.addEventListener(name,e=>{
 window.events[name]++;if(name==='submit')e.preventDefault();
},true);
area.onclick=()=>{window.events.open++;owned.style.display='block'};
document.addEventListener('keydown',e=>{if(e.key==='Escape')owned.style.display='none'});
for(const item of document.querySelectorAll('.ant-cascader-menu-item'))item.onclick=()=>{
 window.events.option++;area.querySelector('input').value='错误选择';
 area.querySelector('input').dispatchEvent(new Event('input',{bubbles:true}));
 area.querySelector('input').dispatchEvent(new Event('change',{bubbles:true}));
};
</script>'''


def calendar(panel, *, metadata='', placeholder='请选择'):
    return r'''<meta charset="utf-8"><form id="fixture-form">
<div id="date" class="ant-picker" ''' + metadata + r'''><div class="ant-picker-input"><input readonly value="" placeholder="''' + placeholder + r'''" aria-controls="calendar-panel"></div></div>
<button type="submit">提交</button></form>
<div id="calendar-panel" class="ant-picker-dropdown" style="display:none">''' + panel + r'''</div>
<script>
window.events={input:0,change:0,submit:0,open:0,option:0};
for(const name of ['input','change','submit'])document.addEventListener(name,e=>{
 window.events[name]++;if(name==='submit')e.preventDefault();
},true);
document.querySelector('#date').onclick=()=>{window.events.open++;document.querySelector('#calendar-panel').style.display='block'};
document.addEventListener('keydown',e=>{if(e.key==='Escape')document.querySelector('#calendar-panel').style.display='none'});
for(const cell of document.querySelectorAll('.ant-picker-cell'))cell.onclick=()=>window.events.option++;
</script>'''


async def assert_read_only(page, *, opens=1):
    events=await page.evaluate('()=>window.events')
    assert events == {'input':0,'change':0,'submit':0,'open':opens,'option':0}, events


async def rejects_cascade(page, setup=None):
    await page.set_content(CASCADE)
    if setup:
        await page.evaluate(setup)
    try:
        await preview_cascade(page,page.locator('#area'))
        raise AssertionError('unsafe or unowned cascade accepted')
    except ValueError:
        pass
    await assert_read_only(page)
    assert await page.locator('#area input').input_value() == '保留的已选路径'


async def cascade_tests(page):
    await page.set_content(CASCADE)
    result=await preview_cascade(page,page.locator('#area'))
    assert result['read_only'] and not result['complete'] and result['options_capture']=='dependent'
    assert result['scope']=='owned_current_visible_layers' and result['observed_layer_count']==2
    assert not result['truncated']
    assert [layer['visible_layer_index'] for layer in result['layers']]==[0,1]
    assert [layer['declared_level'] for layer in result['layers']]==[1,2]
    assert [[option['text'] for option in layer['options']] for layer in result['layers']]==[['北京市','其他'],['海淀区']]
    assert result['layers'][0]['options'][0]['branch'] is True
    assert result['layers'][0]['options'][1]['disabled'] is True
    assert all('selected' not in option and 'marker' not in option for layer in result['layers'] for option in layer['options'])
    wire=json.dumps(result,ensure_ascii=False)
    assert all(value not in wire for value in ['保留的已选路径','邻题选项','隐藏选项','隐藏层','隐藏说明','current_value','value_path'])
    await assert_read_only(page)
    assert not await page.locator('#owned').is_visible()
    assert await page.locator('#neighbour').is_visible()
    assert await page.locator('#area input').input_value()=='保留的已选路径'

    # Broken/multiple ownership cannot borrow an old or unrelated open menu.
    await rejects_cascade(page,"document.querySelector('#area input').setAttribute('aria-controls','missing')")
    await rejects_cascade(page,"document.querySelector('#area input').setAttribute('aria-controls','owned-list neighbour-list')")
    await rejects_cascade(page,"""() => {
      document.querySelector('#area input').removeAttribute('aria-controls');
      area.onclick=()=>window.events.open++;
    }""")
    await rejects_cascade(page,"""() => {
      const duplicate=document.createElement('div');duplicate.className='ant-cascader-dropdown';
      duplicate.innerHTML='<ul id="owned-list" class="ant-cascader-menu"><li class="ant-cascader-menu-item">重复归属</li></ul>';
      document.body.appendChild(duplicate);
    }""")

    # Bound observation budgets report truncation, not a complete option tree.
    await page.set_content(CASCADE)
    await page.evaluate("""() => {
      const first=owned.querySelector('.ant-cascader-menu');
      for(let i=0;i<90;i++){const li=document.createElement('li');li.className='ant-cascader-menu-item';li.textContent='匿名选项'+i;first.appendChild(li)}
    }""")
    bounded=await preview_cascade(page,page.locator('#area'))
    assert bounded['truncated'] and not bounded['complete']
    assert bounded['layers'][0]['visible_option_count']==92 and len(bounded['layers'][0]['options'])==80
    await assert_read_only(page)

    # An eager/default selection by the site's open handler is detected, not
    # silently accepted as a read-only observation. The helper does not undo it.
    await page.set_content(CASCADE)
    await page.evaluate("() => {area.onclick=()=>{window.events.open++;owned.style.display='block';area.querySelector('input').value='网页打开时误改'}}")
    try:
        await preview_cascade(page,page.locator('#area'))
        raise AssertionError('open-time value mutation accepted')
    except ValueError as exc:
        assert '填写值发生变化' in str(exc)
    await assert_read_only(page)

    # Credential/file/disabled impostors fail before the opening click.
    for setup in ["area.querySelector('input').type='password'", "area.querySelector('input').type='file'", "area.setAttribute('aria-disabled','true')"]:
        await page.set_content(CASCADE)
        await page.evaluate(setup)
        await page.evaluate("""() => {
          window.unsafe_reads=0;
          Object.defineProperty(area.querySelector('input'),'value',{get:()=>{window.unsafe_reads++;throw Error('must not read rejected values')}});
        }""")
        try:
            await preview_cascade(page,page.locator('#area'))
            raise AssertionError('unsafe control was opened')
        except ValueError:
            pass
        await assert_read_only(page,opens=0)
        assert await page.evaluate('()=>window.unsafe_reads')==0


async def calendar_tests(page):
    date_panel='<div class="ant-picker-panel"><div class="ant-picker-date-panel"><table><tr><td class="ant-picker-cell" title="2001-02-03">3</td></tr></table></div></div>'
    month_panel='<div class="ant-picker-panel"><div class="ant-picker-month-panel">月份</div><div class="ant-picker-date-panel" hidden>隐藏日期</div></div>'
    for panel,metadata,placeholder,expected,opens in [
            (date_panel,'','请选择','date',1),
            (month_panel,'data-format="YYYY-MM"','请选择','month',1),
            (month_panel,'','YYYY/MM','month',1),
            (month_panel,'data-format="YYYY-MM-DD"','请选择','',1),
            (date_panel,'data-format="DD/MM/YYYY"','请选择','',0),
            (date_panel,'data-format="YYYY-MM-DD" data-date-format="YYYY-MM"','请选择','',0),
            (date_panel+'<div class="ant-picker-week-panel">周</div>','','请选择','',1),
            (date_panel+'<div class="ant-picker-time-panel">时间</div>','','请选择','',1),
            (date_panel+'<div class="ant-picker-panel">另一个日期</div>','','请选择','',1),
            ('<div class="ant-picker-panel"><div class="ant-picker-date-panel">日</div><div class="ant-picker-month-panel">月</div></div>','','请选择','',1),
    ]:
        await page.set_content(calendar(panel,metadata=metadata,placeholder=placeholder))
        assert await preview_calendar(page,page.locator('#date'))==expected
        await assert_read_only(page,opens=opens)
        assert await page.locator('#date input').input_value()==''
        assert not await page.locator('#calendar-panel').is_visible()

    # Native precision is proved by HTML type; Ant metadata is owned format,
    # never a date-like question caption or an unverified field_type.
    await page.set_content('''<label>出生日期</label><input id="text">
      <input id="native-date" type="date"><input id="native-month" type="month">
      <div id="phantom"></div><div id="range" class="ant-picker ant-picker-range"><input></div>
      <div id="metadata" class="ant-picker" data-format="YYYY.MM"><input></div>''')
    fields=[{'selector':'#text','field_type':'text'},{'selector':'#native-date','field_type':'date'},
        {'selector':'#native-month','field_type':'month'},{'selector':'#phantom','field_type':'date'},
        {'selector':'#range','field_type':'combobox'},{'selector':'#metadata','field_type':'combobox'}]
    await classify_controls(page,fields)
    assert [field['control_kind'] for field in fields]==['text','date','month','unknown','unknown','calendar']
    assert [fields[index].get('date_precision','') for index in range(6)]==['','date','month','','','month']
    await page.set_content(calendar(date_panel,placeholder='DD/MM/YYYY'))
    await page.locator('#date').evaluate("el=>el.dataset.zhidaDatePrecision='date'")
    stale=[{'selector':'#date','field_type':'combobox'}]
    await classify_controls(page,stale)
    assert stale[0]['date_precision']=='', 'unsupported explicit format must override old precision cache'
    assert await preview_calendar(page,page.locator('#date'))==''
    await assert_read_only(page,opens=0)
    assert await page.locator('#date').get_attribute('data-zhida-date-precision') is None
    await page.set_content(calendar(date_panel))
    assert await preview_calendar(page,page.locator('#date'))=='date'
    await page.locator('#calendar-panel').evaluate("el=>el.innerHTML='<div class=\"ant-picker-time-panel\">时间</div>'")
    assert await preview_calendar(page,page.locator('#date'))==''
    fresh=[{'selector':'#date','field_type':'combobox'}]
    await classify_controls(page,fresh)
    assert fresh[0]['date_precision']=='', 'an unsupported fresh panel must erase old precision cache'
    await assert_read_only(page,opens=2)


async def run():
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(headless=True,channel='chrome')
        context=await browser.new_context()
        await context.route('**/*',lambda route:route.abort())
        await context.route_web_socket('**/*',lambda route:route.close())
        page=await context.new_page()
        page.set_default_timeout(5000)
        await cascade_tests(page)
        await calendar_tests(page)
        await browser.close()
    print('structured_controls_preview_test: OK (owned visible layers only, no path/complete claim, stale/ARIA/unsafe guards, value mutation stop, DOM format precision; zero input/change/submit/option clicks)')


if __name__=='__main__':
    asyncio.run(run())
