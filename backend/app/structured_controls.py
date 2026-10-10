"""Typed, owned widget observations shared by the mapper and executor.

Never infer a calendar from a date-like label or turn a cascade into a flat
option list. Only the bound page is used; no component state/private props,
arbitrary code from a model, default dates, or global option search.
"""
from __future__ import annotations

import re
from uuid import uuid4

from .phoenix_calendar import canonical_date


async def classify_controls(page, fields):
    observations = await page.evaluate(r"""fields => fields.map(field => {
      const el=document.querySelector(field.selector);
      if(!el)return {};
      const input=el.matches('input')?el:el.querySelector('input');
      const type=(input?.type||'').toLowerCase();
      if(el.matches('.ant-picker-range,.ant-calendar-range-picker'))
        return {control_kind:'unknown',date_precision:'',control_evidence:'当前控件为日期范围，不按单个年月或年月日处理'};
      if(el.matches('.ant-calendar-picker,.ant-picker')){
        const formatPrecision=value=>/^YYYY[-/.]MM[-/.]DD$/.test(value)?'date':/^YYYY[-/.]MM$/.test(value)?'month':'';
        const formats=[el,input].filter(Boolean).flatMap(n=>['data-format','data-date-format'].map(key=>n.getAttribute(key)||''));
        const placeholder=String(input?.getAttribute('placeholder')||'').trim().toUpperCase();
        if(formatPrecision(placeholder)||/YYYY|DD|HH|SS|WW/.test(placeholder))formats.push(placeholder);
        const explicit=formats.map(value=>String(value).trim().toUpperCase()).filter(Boolean);
        const declared=[...new Set(explicit.map(formatPrecision))];
        const cached=['date','month'].includes(el.dataset.zhidaDatePrecision)?el.dataset.zhidaDatePrecision:'';
        const precision=declared.length===1&&declared[0]?declared[0]:!explicit.length?cached:'';
        return {control_kind:'calendar',date_precision:precision,control_evidence:precision?
          '当前 Ant 日历的精度来自控件自身格式 metadata 或已核实面板，不来自题干':
          '当前控件使用 Ant 日历容器；展开后核实精度，不是选项列表'};
      }
      if(el.matches('.ant-cascader-picker,.ant-cascader'))
        return {control_kind:'cascade',control_evidence:'当前控件使用级联容器；省、市、区或其他层级分别读取'};
      if(el.matches('select'))return {control_kind:'native_select',control_evidence:'HTML select，按真实 option 选择'};
      if(['date','month'].includes(type))return {control_kind:type,date_precision:type,control_evidence:'HTML '+type+' 输入，精度来自控件类型'};
      if(['radio','checkbox','file'].includes(field.field_type))return {control_kind:field.field_type,control_evidence:'网页原生/ARIA '+field.field_type+' 控件'};
      if(field.field_type==='combobox')return {control_kind:'dropdown',control_evidence:'网页 combobox，需要读取归属于当前题目的真实选项'};
      if ((el.matches('textarea') || el.matches('input')&&['text','tel','email','url','number','search'].includes(type)) &&
          !el.readOnly && !el.disabled)
        return {control_kind:'text',control_evidence:'已核实可编辑的 HTML 文本/数字输入；执行后回读核验'};
      if(field.field_type==='section-button')return {control_kind:'section',control_evidence:'展开栏目按钮，不是资料输入框'};
      return {control_kind:'unknown',control_evidence:'尚未核实可编辑输入或已支持的控件结构，不按普通文本框处理'};
    })""", [{"selector":f['selector'],"field_type":f['field_type']} for f in fields])
    for field, observation in zip(fields, observations):
        field.update(observation or {'control_kind':'unknown','control_evidence':'定位到的控件已经不存在，需要重新读取'})


POPUPS = '.ant-calendar,.ant-picker-dropdown,.ant-cascader-menus,.ant-cascader-dropdown'


async def popup_ids(page):
    return await page.evaluate(r"""({selector,prefix}) => {
      const visible=n=>n.getClientRects().length&&getComputedStyle(n).display!=='none'&&
        !n.closest('[hidden],[aria-hidden="true"]')&&!['hidden','collapse'].includes(getComputedStyle(n).visibility);
      const nodes=[...document.querySelectorAll(selector)].filter(visible);
      return nodes.filter(n=>!nodes.some(p=>p!==n&&p.contains(n))).map((n,i)=>{
        n.dataset.zhidaStructuredPopup ||= prefix+'-'+i; return n.dataset.zhidaStructuredPopup;
      });
    }""", {'selector':POPUPS,'prefix':uuid4().hex})


async def owned_popup(page, control, before, kind):
    result = await control.evaluate(r"""(el,{before,selector,kind,prefix}) => {
      const visible=n=>n.getClientRects().length&&getComputedStyle(n).display!=='none'&&
        !n.closest('[hidden],[aria-hidden="true"]')&&!['hidden','collapse'].includes(getComputedStyle(n).visibility);
      const ids=[...new Set([el,...el.querySelectorAll('[aria-controls],[aria-owns]')].flatMap(n=>
        (String(n.getAttribute('aria-controls')||'')+' '+String(n.getAttribute('aria-owns')||'')).trim().split(/\s+/).filter(Boolean)))];
      const matchesKind=n=>kind==='calendar'?n.matches('.ant-calendar,.ant-picker-dropdown'):n.matches('.ant-cascader-menus,.ant-cascader-dropdown');
      const nodes=[...document.querySelectorAll(selector)].filter(n=>visible(n)&&matchesKind(n));
      const outer=nodes.filter(n=>!nodes.some(p=>p!==n&&p.contains(n)));
      // Broken ARIA ownership is not permission to use another question's panel.
      const candidates=ids.length?outer.filter(n=>ids.some(id=>n.id===id||n.querySelector('[id="'+CSS.escape(id)+'"]'))):
        outer.filter(n=>el.contains(n)||!before.includes(n.dataset.zhidaStructuredPopup));
      if(candidates.length!==1)return null;
      const panel=candidates[0]; panel.dataset.zhidaStructuredPopup ||= prefix;
      return panel.dataset.zhidaStructuredPopup;
    }""", {'before':before,'selector':POPUPS,'kind':kind,'prefix':uuid4().hex})
    return page.locator(f'[data-zhida-structured-popup="{result}"]') if result else None


async def open_owned_popup(page, control, kind, before_write=None):
    # Close an existing owned/open menu via its focused control, not global
    # clicking, clearing a value, or blindly treating the sole open menu as ours.
    await control.press('Escape')
    before=await popup_ids(page)
    if before_write:
        await before_write()
    await control.click(timeout=4000)
    for _ in range(3):
        panel=await owned_popup(page, control, before, kind)
        if panel is not None:
            return panel
        await page.wait_for_timeout(150)
    raise ValueError('未能唯一核实当前控件展开的面板；已停止，不会使用其他题目的选项')


async def ant_calendar_precision(panel):
    return await panel.evaluate(r"""panel => {
      const visible=n=>{
        if(!n.getClientRects().length||n.closest('[hidden],[aria-hidden="true"]'))return false;
        for(let p=n;p;p=p.parentElement)if(getComputedStyle(p).display==='none'||
          ['hidden','collapse'].includes(getComputedStyle(p).visibility))return false;
        return true;
      };
      const count=selector=>[...panel.querySelectorAll(selector)].filter(visible).length;
      const formatPrecision=value=>/^YYYY[-/.]MM[-/.]DD$/.test(value)?'date':/^YYYY[-/.]MM$/.test(value)?'month':'';
      // A range/time/week/custom picker is not an ordinary date input.
      if(panel.matches('.ant-picker-dropdown')){
        if(panel.matches('.ant-picker-dropdown-range')||count('.ant-picker-time-panel,.ant-picker-week-panel')||
          count('.ant-picker-panel')>1)return '';
        const date=count('.ant-picker-date-panel'),month=count('.ant-picker-month-panel');
        return date===1&&!month?'date':month===1&&!date?'month':'';
      }
      if(panel.matches('.ant-calendar-range')||count('.ant-calendar-range,.ant-calendar-time-picker,.ant-calendar-week-number'))return '';
      const inputs=[...panel.querySelectorAll('input.ant-calendar-input')].filter(visible);
      if(inputs.length>1)return '';
      const input=inputs[0];
      const fmt=String(input?.placeholder||'').toUpperCase().trim();
      const declared=formatPrecision(fmt);
      const date=count('.ant-calendar-date-panel,.ant-calendar-table')>0,month=count('.ant-calendar-month-panel')>0;
      if(date&&month||declared==='date'&&month||declared==='month'&&date)return '';
      if(declared)return declared;
      if(fmt&&/YYYY|DD|HH|SS|WW/.test(fmt))return ''; // explicit unsupported format
      return date?'date':month?'month':'';
    }""")


async def _preview_value_state(control, kind):
    """Internal equality guard only; never attach these values to observations."""
    state=await control.evaluate(r"""(el,kind)=>{
      const supported=kind==='cascade'?el.matches('.ant-cascader-picker,.ant-cascader'):
        el.matches('.ant-calendar-picker,.ant-picker')&&!el.matches('.ant-picker-range,.ant-calendar-range-picker');
      const nodes=[el,...el.querySelectorAll('input,textarea,select,[contenteditable="true"]')]
        .filter(n=>!n.closest('.ant-calendar,.ant-picker-dropdown,.ant-cascader-menus,.ant-cascader-dropdown'));
      const unsafe=nodes.some(n=>n.matches('input[type="password"],input[type="file"]')||
        /^(current-password|new-password|one-time-code)$/.test(n.getAttribute('autocomplete')||''));
      const disabled=el.matches('[disabled],[aria-disabled="true"],.ant-cascader-disabled,.ant-picker-disabled')||
        nodes.some(n=>n.matches('input:disabled,select:disabled,textarea:disabled'));
      if(!supported||unsafe||disabled)return {supported,unsafe,disabled};
      return {supported,unsafe,disabled,
        identity:[el.tagName,el.id,el.getAttribute('name')||''],
        displayed_selection:[...el.querySelectorAll('.ant-cascader-picker-label,.ant-select-selection-item,.ant-select-selection-selected-value')]
          .map(n=>String(n.textContent||'')),
        values:nodes.map(n=>[n.tagName,n.getAttribute('type')||'',n.getAttribute('name')||'',
          'value' in n?String(n.value):n.isContentEditable?String(n.textContent||''):'',
          ['checkbox','radio'].includes(n.type)?n.checked:null,
          n.matches('select')?[...n.options].map(option=>option.selected):null])};
    }""", kind)
    if not state['supported'] or state['unsafe'] or state['disabled']:
        raise ValueError('当前控件不是可安全展开的已支持日历或级联控件；未点击或读取选项')
    return state


async def _finish_preview(control, kind, before):
    await control.press('Escape')
    if await _preview_value_state(control, kind) != before:
        raise ValueError('只读展开期间控件身份或填写值发生变化；已丢弃观察结果，请重新核对')


async def preview_calendar(page, control):
    before=await _preview_value_state(control, 'calendar')
    metadata=await control.evaluate(r"""el=>{
      const input=el.querySelector('input');
      const formatPrecision=value=>/^YYYY[-/.]MM[-/.]DD$/.test(value)?'date':/^YYYY[-/.]MM$/.test(value)?'month':'';
      const formats=[el,input].filter(Boolean).flatMap(n=>['data-format','data-date-format'].map(key=>n.getAttribute(key)||''));
      const placeholder=String(input?.getAttribute('placeholder')||'').trim().toUpperCase();
      if(formatPrecision(placeholder)||/YYYY|DD|HH|SS|WW/.test(placeholder))formats.push(placeholder);
      const explicit=formats.map(value=>String(value).trim().toUpperCase()).filter(Boolean);
      const declared=[...new Set(explicit.map(formatPrecision))];
      return {blocked:declared.includes('')||declared.length>1,precision:declared.length===1?declared[0]:''};
    }""")
    if metadata['blocked']:
        await control.evaluate('el=>delete el.dataset.zhidaDatePrecision')
        return ''
    try:
        panel=await open_owned_popup(page, control, 'calendar')
        precision=await ant_calendar_precision(panel)
        if metadata['precision'] and metadata['precision'] != precision:
            precision=''
        await control.evaluate('(el,precision)=>{if(precision)el.dataset.zhidaDatePrecision=precision;else delete el.dataset.zhidaDatePrecision}',precision)
        return precision
    finally:
        await _finish_preview(control, 'calendar', before)


async def preview_cascade(page, control):
    """Observe rendered layers of ONE owned cascade; never explore its tree.

    Opening the control is the only permitted click. Neither an option nor a
    next-layer node is clicked/hovered. A visible column is not a full path,
    and a finite rendered list is not proof that lazy/virtual options are done.
    """
    before=await _preview_value_state(control, 'cascade')
    try:
        panel=await open_owned_popup(page, control, 'cascade')
        layers=await panel.evaluate(r"""panel=>{
          const visible=n=>{
            if(!n.getClientRects().length||n.closest('[hidden],[aria-hidden="true"]'))return false;
            for(let p=n;p;p=p.parentElement)if(getComputedStyle(p).display==='none'||
              ['hidden','collapse'].includes(getComputedStyle(p).visibility))return false;
            return true;
          };
          const columns=[...panel.querySelectorAll('.ant-cascader-menu')].filter(visible);
          return {observed_layer_count:columns.length,layers:columns.slice(0,5).map((column,index)=>{
            const options=[...column.children].filter(n=>n.matches('.ant-cascader-menu-item')&&visible(n));
            const level=column.getAttribute('aria-level')||'';
            return {visible_layer_index:index,declared_level:/^[1-9]\d?$/.test(level)?Number(level):null,
              visible_option_count:options.length,truncated:options.length>80,options:options.slice(0,80).map(node=>{
                const copy=node.cloneNode(true);
                const originals=[...node.querySelectorAll('*')],copies=[...copy.querySelectorAll('*')];
                originals.forEach((child,i)=>{if(!visible(child))copies[i]?.remove()});
                copy.querySelectorAll('svg,[class*="expand-icon"],input,textarea,select,[hidden],[aria-hidden="true"],script,style').forEach(n=>n.remove());
                const text=String(copy.textContent||'').replace(/\s+/g,' ').trim();
                return {text:text.slice(0,240),text_truncated:text.length>240,
                  disabled:node.matches('[disabled],[aria-disabled="true"],.ant-cascader-menu-item-disabled')||
                    Boolean(node.closest('[aria-disabled="true"]')),
                  branch:node.matches('.ant-cascader-menu-item-expand')||node.getAttribute('aria-haspopup')==='true'};
              })};
          })};
        }""")
        return {'control_kind':'cascade','read_only':True,'scope':'owned_current_visible_layers',
            'options_capture':'dependent','layers':layers['layers'],
            'observed_layer_count':layers['observed_layer_count'],
            'truncated':layers['observed_layer_count']>5 or any(layer['truncated'] or
                any(option['text_truncated'] for option in layer['options']) for layer in layers['layers']),
            'complete':False,'limitations':[
                '只读取当前归属面板已呈现的可见层和选项，不代表完整树或全量选项',
                '没有选择或探测任何节点，没有读取未展开、懒加载或虚拟列表中的下级',
                '可见列序号只表示当前渲染顺序，不能拼接或推断完整路径',
            ]}
    finally:
        await _finish_preview(control, 'cascade', before)


async def click_readonly_calendar_cell(panel, value, before_write=None):
    """Select an actual enabled cell, never remove readonly or set its value.

    This deliberately only handles a date already rendered by the owned
    panel. Unsupported navigation remains a visible failure, not guessing.
    """
    marker = await panel.evaluate(r"""(panel,{value,prefix}) => {
      const visible=n=>n.getClientRects().length&&!n.closest('[hidden],[aria-hidden="true"]')&&
        getComputedStyle(n).display!=='none'&&!['hidden','collapse'].includes(getComputedStyle(n).visibility);
      const cells=[...panel.querySelectorAll('.ant-picker-cell[title]')].filter(n=>
        visible(n)&&n.getAttribute('title')===value&&!n.matches('.ant-picker-cell-disabled,[aria-disabled="true"]')&&
        !n.closest('[aria-disabled="true"]'));
      if(cells.length!==1)return '';
      const inner=[...cells[0].querySelectorAll('.ant-picker-cell-inner')].filter(visible);
      if(inner.length>1)return '';
      const target=inner[0]||cells[0];target.dataset.zhidaCalendarCell=prefix;
      return prefix;
    }""", {'value':value,'prefix':uuid4().hex})
    if not marker:
        raise ValueError('只读日历中尚未找到唯一、可选的目标日期；请定位到对应年月，不会强写只读值')
    if before_write:
        await before_write()
    await panel.locator(f'[data-zhida-calendar-cell="{marker}"]').click(timeout=4000)


async def select_ant_calendar(page, control, wanted, precision, before_write=None):
    value=canonical_date(wanted, precision)
    panel=await open_owned_popup(page, control, 'calendar', before_write)
    try:
        if await ant_calendar_precision(panel)!=precision:
            raise ValueError('当前日历精度与计划不同，请重新核对，不会使用旧日期计划')
        modern=await panel.evaluate("el=>el.matches('.ant-picker-dropdown')")
        if modern:
            if not await control.evaluate("el=>el.matches('.ant-picker')&&!el.matches('.ant-picker-range')"):
                raise ValueError('日历面板与外层控件不一致，已停止')
            entry=control.locator('.ant-picker-input > input')
        else:
            entry=panel.locator('input.ant-calendar-input')
        if await entry.count()!=1:
            raise ValueError('当前日历尚无经过验证的日期输入方式，请在官网核对；不会强写只读值')
        if not await entry.is_editable():
            if not modern or not await entry.evaluate('el=>el.readOnly&&!el.disabled'):
                raise ValueError('当前日历输入不可操作，已停止；不会强写只读值')
            await click_readonly_calendar_cell(panel, value, before_write)
        else:
            # Enter is limited to this proven calendar input. Suppress form
            # submit while it commits; still verify the selected cell below.
            guard=await entry.evaluate_handle(r"""input => {
              const stop=e=>{e.preventDefault();e.stopImmediatePropagation()};
              document.addEventListener('submit',stop,true);
              return ()=>document.removeEventListener('submit',stop,true);
            }""")
            try:
                if before_write:
                    await before_write()
                await entry.fill(value,timeout=4000)
                await entry.press('Enter',timeout=4000)
            finally:
                await guard.evaluate('cleanup=>cleanup()')
                await guard.dispose()
        await page.wait_for_timeout(350)
        if modern:
            # Typing alone is not a selected date. Reopen only this owned panel
            # and prove its selected calendar cell, not React/private state.
            await control.press('Escape')
            committed_panel=await open_owned_popup(page,control,'calendar',before_write)
            selected=await committed_panel.evaluate(r"""(panel,wanted)=>{
              const cells=[...panel.querySelectorAll('.ant-picker-cell-selected[title]')];
              return cells.filter(n=>n.getClientRects().length&&n.getAttribute('title')===wanted).length===1;
            }""", value)
            if not selected:
                raise ValueError('日期输入未形成对应的日历选中状态，不计为填写成功，请核对官网格式或限制')
            if await entry.input_value()!=value:
                raise ValueError('日历选中状态与输入回读不一致，不计为填写成功')
        return value
    finally:
        await control.press('Escape')


async def cascade_columns(panel):
    return await panel.evaluate(r"""(panel,prefix)=>{
      const visible=n=>n.getClientRects().length&&!n.closest('[hidden],[aria-hidden="true"]');
      const cols=[...panel.querySelectorAll('.ant-cascader-menu')].filter(visible);
      return cols.map((col,j)=>[...col.children].filter(n=>n.matches('.ant-cascader-menu-item')&&visible(n)).map((n,i)=>{
        n.dataset.zhidaCascadeOption=prefix+'-'+j+'-'+i;
        const c=n.cloneNode(true);c.querySelectorAll('svg,[class*="expand-icon"]').forEach(n=>n.remove());
        return {text:String(c.textContent||'').replace(/\s+/g,' ').trim(),marker:n.dataset.zhidaCascadeOption,
          disabled:n.matches('[aria-disabled="true"],.ant-cascader-menu-item-disabled'),
          branch:n.matches('.ant-cascader-menu-item-expand')||n.getAttribute('aria-haspopup')==='true'};
      }));
    }""", uuid4().hex)


async def select_ant_cascade(page, control, wanted, match_option, before_write=None):
    tokens=[t.strip() for t in re.split(r'\s*(?:/|>|→|、|，|,)\s*',wanted) if t.strip()]
    if not tokens or len(tokens)>5:
        raise ValueError('级联选择需要明确的完整路径，不会推测省市区或其他层级')
    panel=await open_owned_popup(page,control,'cascade',before_write)
    selected=[]
    try:
        for depth, target in enumerate(tokens):
            columns=await cascade_columns(panel)
            if len(columns)<=depth:
                raise ValueError('级联下一层尚未出现，请重新核对，不会使用相邻题目选项')
            options=[item for item in columns[depth] if not item['disabled']]
            match=match_option(target,[item['text'] for item in options])
            choices=[item for item in options if item['text']==match] if match else []
            if len(choices)!=1:
                raise ValueError('当前级联层级无法唯一对应已确认路径；真实选项：'+'、'.join(i['text'] for i in options[:20]))
            item=choices[0]
            if depth==len(tokens)-1 and item['branch']:
                raise ValueError('提供的路径未到最终可选层级，请补充具体下级选项；不会擅自选区县')
            if depth<len(tokens)-1 and not item['branch']:
                raise ValueError('官网级联层级少于提供路径，请核对当前真实选项')
            if before_write:
                await before_write()
            await page.locator(f'[data-zhida-cascade-option="{item["marker"]}"]').click(timeout=4000)
            selected.append(item['text'])
            await page.wait_for_timeout(250)
        return [' / '.join(selected)]
    finally:
        await control.press('Escape')
