"""Observed Phoenix calendar input, never generic Enter on an ATS form.

Format is proven by the opened widget, not a question's date-like caption.
Typing in its search/input area is not successful until the outer control
contains the same committed date after settlement.
"""
from datetime import date
import re


async def visible_calendars(page):
    return await page.locator('.phoenix-date-picker:visible').all()


async def calendar_for(control, page, before_ids):
    candidates = await visible_calendars(page)
    owned, fresh = [], []
    handle = await control.element_handle()
    try:
        for panel in candidates:
            if await panel.evaluate('(panel, control) => control.closest(".phoenix-unmodeled-layer")?.contains(panel) || control.closest(".form-item.form-item--phoenix")?.contains(panel)', handle):
                owned.append(panel)
            if await panel.get_attribute('data-zhida-calendar-id') not in before_ids:
                fresh.append(panel)
    finally:
        if handle:
            await handle.dispose()
    matches = owned if owned else fresh
    return matches[0] if len(matches) == 1 else None


async def calendar_ids(page):
    result = set()
    for panel in await visible_calendars(page):
        marker = await panel.evaluate('el => el.dataset.zhidaCalendarId || (el.dataset.zhidaCalendarId = crypto.randomUUID())')
        result.add(marker)
    return result


async def calendar_precision(panel):
    if panel is None:
        return ''
    if (await panel.locator('.phoenix-calendar-month-panel-table:visible').count() == 1
            and not await panel.locator('.phoenix-calendar-date-panel .phoenix-calendar-table:visible').count()):
        return 'month'
    inputs = panel.locator('input.phoenix-calendar-input:visible')
    if await inputs.count() != 1:
        return ''
    entry = inputs.first
    if not await entry.is_editable():
        return ''
    placeholder = (await entry.get_attribute('placeholder') or '').strip().upper()
    if re.fullmatch(r'YYYY[-/.]MM[-/.]DD', placeholder):
        return 'date'
    if re.fullmatch(r'YYYY[-/.]MM', placeholder):
        return 'month'
    if await panel.locator('.phoenix-calendar-date-panel .phoenix-calendar-table:visible').count() == 1:
        return 'date'
    return ''


def canonical_date(value, precision):
    match = re.fullmatch(r'(\d{4})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?', str(value).strip())
    if not match:
        raise ValueError('请提供真实的年-月或年-月-日，不会猜测日期')
    year, month, day = int(match[1]), int(match[2]), match[3]
    if precision == 'date' and day is None:
        raise ValueError('官网要求年月日，现有资料只有年月；请确认真实日期，不会擅自补为每月1日')
    date(year, month, int(day) if day is not None else 1)  # validate, not invent a stored day
    if precision == 'month':
        return f'{year:04d}-{month:02d}'
    if precision == 'date':
        return f'{year:04d}-{month:02d}-{int(day):02d}'
    raise ValueError('尚未核实官网日历精度，请重新定位日期控件')


async def select_calendar_date(page, control, wanted, precision, before_write):
    before = await calendar_ids(page)
    if before_write:
        await before_write()
    await control.click(timeout=4000)
    panel = None
    for _ in range(10):
        panel = await calendar_for(control, page, before)
        if panel is not None:
            break
        await page.wait_for_timeout(100)
    actual_precision = await calendar_precision(panel)
    if not actual_precision or actual_precision != precision:
        raise ValueError('官网日历格式或归属已变化，已停止旧日期计划')
    value = canonical_date(wanted, precision)
    if precision == 'month' and await panel.locator('.phoenix-calendar-month-panel-table:visible').count() == 1:
        year, month = map(int, value.split('-'))
        header = panel.locator('.phoenix-calendar-month-panel-year-select-content:visible')
        if await header.count() != 1:
            raise ValueError('无法唯一核实日历当前年份，已停止日期填写')
        current_text = (await header.inner_text()).strip()
        match = re.fullmatch(r'(\d{4})\s*年?', current_text)
        if not match or abs(int(match[1]) - year) > 40:
            raise ValueError('日历年份无法核实或距离过远，请在官网核对后重试')
        current = int(match[1])
        for _ in range(abs(year-current)):
            button = panel.locator('.phoenix-calendar-month-panel-' + ('prev' if current > year else 'next') + '-year-btn:visible')
            if await button.count() != 1 or await button.is_disabled():
                raise ValueError('当前日历不允许选择目标年份')
            if before_write:
                await before_write()
            old = current
            await button.click(timeout=3000)
            for _ in range(10):
                text = (await header.inner_text()).strip()
                match = re.fullmatch(r'(\d{4})\s*年?', text)
                if match and int(match[1]) != old:
                    current = int(match[1])
                    break
                await page.wait_for_timeout(100)
            if current != old + (1 if year > old else -1):
                raise ValueError('切换日历年份未出现预期变化，不会重复点击')
        month_names = ['一月','二月','三月','四月','五月','六月','七月','八月','九月','十月','十一月','十二月']
        matches = []
        for choice in await panel.locator('a.phoenix-calendar-month-panel-month:visible').all():
            text = (await choice.inner_text()).strip()
            if text not in {f'{month}月', month_names[month-1]}:
                continue
            blocked = await choice.evaluate("el=>el.closest('[aria-disabled=\"true\"],[disabled]') || /disabled/i.test(el.closest('td')?.className||'')")
            if not blocked:
                matches.append(choice)
        if len(matches) != 1:
            raise ValueError('目标月份无法在当前日历中唯一匹配，请人工核对')
        if before_write:
            await before_write()
        await matches[0].click(timeout=3000)
        await page.wait_for_timeout(500)
        return value
    entry = panel.locator('input.phoenix-calendar-input:visible')
    if await entry.count() != 1:
        raise ValueError('无法唯一定位当前日期输入框')
    if before_write:
        await before_write()
    await entry.fill(value, timeout=4000)
    # Enter belongs only to the proven calendar input. Never press Enter on
    # the recruiting form, search field, hidden input or arbitrary textbox.
    guard = await entry.evaluate_handle(r'''input => {
      const panel=input.closest('.phoenix-date-picker');
      const stop=event=>{
        const button=event.target?.closest?.('button,input[type="submit"],[role="button"]');
        const final=button&&!panel.contains(button)&&(
          button.type==='submit'||/^(提交申请|提交简历|确认提交|预览并提交|submit application)$/i.test(String(button.innerText||button.value||'').trim()));
        if(event.type==='submit'||final){event.preventDefault();event.stopImmediatePropagation();}
      };
      document.addEventListener('submit',stop,true);
      document.addEventListener('click',stop,true);
      return ()=>{document.removeEventListener('submit',stop,true);document.removeEventListener('click',stop,true);};
    }''')
    try:
        await entry.press('Enter', timeout=4000)
    finally:
        await guard.evaluate('cleanup=>cleanup()')
        await guard.dispose()
    await page.wait_for_timeout(500)
    return value
