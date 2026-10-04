"""Owned Phoenix area picker: drill, select, confirm, then caller reads back.

No global confirmation button or guessed geography. Locators come from the
observed area/breadcrumb/radio structure and a proven scoped option row.
"""
from __future__ import annotations

from uuid import uuid4

from .ats_controls import scoped_option_entries, visible_popup_ids
from .region_facts import region_path


async def audited_region_path(control, actual):
    """Transient write evidence, invalidated by any later manual interaction.

    Some ATS panels forget their checked tree when reopened. Do not pretend a
    leaf string proves a parent. Require our guarded, owned traversal+confirm
    evidence and the same committed outer value. Not profile/answer memory.
    """
    proof = await control.evaluate(r"""el => ({
      path:el.dataset.zhidaRegionCommittedPath||'',
      value:el.dataset.zhidaRegionCommittedValue||'',
      epoch:el.dataset.zhidaRegionEpoch||'',
      committedEpoch:el.dataset.zhidaRegionCommittedEpoch||'',
      title:el.dataset.zhidaRegionCommittedTitle||'',
      currentTitle:String(el.closest('.form-item--phoenix')?.querySelector('.form-item__title')?.innerText||'').trim()
    })""")
    path = proof['path']
    if (not path or proof['epoch'] != proof['committedEpoch'] or proof['value'] != actual
            or proof['title'] != proof['currentTitle'] or not actual):
        return ''
    parts = path.split('/')
    return path if region_path(path) == parts and (actual.strip() == parts[-1] or actual.strip() == path) else ''


async def _watch_region_interactions(control, root):
    await control.evaluate(r"""el => {
      if(el.dataset.zhidaRegionWatch)return;
      el.dataset.zhidaRegionWatch='true';el.dataset.zhidaRegionEpoch='0';
      const invalidate=event=>{
        if(event.type==='keydown'&&['Escape','Tab'].includes(event.key))return;
        el.dataset.zhidaRegionEpoch=String(Number(el.dataset.zhidaRegionEpoch||0)+1);
      };
      for(const type of ['pointerdown','keydown','input','change'])el.addEventListener(type,invalidate,true);
    }""")
    if root is not None:
        handle = await control.element_handle()
        try:
            await root.evaluate(r"""(root, el) => {
              root._zhidaRegionOwner=el;
              if(root.dataset.zhidaRegionWatch)return;root.dataset.zhidaRegionWatch='true';
              const invalidate=event=>{
                if(event.type==='keydown'&&['Escape','Tab'].includes(event.key))return;
                const owner=root._zhidaRegionOwner;
                if(owner)owner.dataset.zhidaRegionEpoch=String(Number(owner.dataset.zhidaRegionEpoch||0)+1);
              };
              for(const type of ['pointerdown','keydown'])root.addEventListener(type,invalidate,true);
            }""", handle)
        finally:
            await handle.dispose()


async def read_open_region_path(page, control, entries, actual):
    """Prove a leaf-only display from its own checked menu and breadcrumbs."""
    if not actual or not entries:
        return ''
    root = await _owned_root(page, entries[0][1])
    if root is None:
        return ''
    checked = [(text, node) for text, node in entries
               if await node.locator('.icon-container.visible svg.area-icon-RadioChecked').count() == 1]
    if len(checked) != 1:
        return ''
    leaf, _ = checked[0]
    crumbs = [text.strip() for text in await root.locator('.phoenix-breadcrumb-text').all_text_contents()
              if text.strip() and text.strip() != '全国']
    path = crumbs if crumbs and crumbs[-1] == leaf else [*crumbs, leaf]
    joined = '/'.join(path)
    # Every component must be an explicit administrative level. A district
    # caption alone cannot reveal the province; a guessed parent never counts.
    if region_path(joined) != path:
        return ''
    from .region_facts import region_values_match
    if actual.strip() != leaf and not region_values_match(joined, actual):
        return ''
    return joined


async def _owned_root(page, option):
    marker = uuid4().hex
    found = await option.evaluate(r"""(el, marker) => {
      const visible=n=>n.getClientRects().length&&!n.closest('[hidden],[aria-hidden="true"]');
      const clean=n=>String(n.innerText||'').replace(/\s+/g,' ').trim();
      for(let n=el.parentElement,d=0;n&&d<12&&n!==document.body;n=n.parentElement,d++){
        if(n.matches('form,.form-item--phoenix')||n.querySelector('form'))break;
        const panels=[...n.querySelectorAll('.area-data-container')].filter(visible);
        const crumbs=[...n.querySelectorAll('.area-breadcrumb-list')].filter(visible);
        const buttons=[...n.querySelectorAll('.phoenix-button')].filter(b=>visible(b)&&clean(b)==='确定');
        if(panels.length===1&&panels[0].contains(el)&&crumbs.length===1&&buttons.length===1&&
           !n.querySelector('.form-item--phoenix,input[type="submit"],button[type="submit"]')){
          n.dataset.zhidaRegionRoot=marker;return true;
        }
      }return false;
    }""", marker)
    return page.locator(f'[data-zhida-region-root="{marker}"]') if found else None


async def select_region(page, control, value, policy, best_option, before_write=None):
    path = region_path(value) or [value.strip()]
    if not path or not path[0]:
        raise ValueError('地区没有明确值，已停止填写')
    before = await visible_popup_ids(page, policy)
    if before_write:
        await before_write()
    await control.scroll_into_view_if_needed(timeout=3000)
    await control.click(timeout=8000)
    entries = []
    for _ in range(8):
        entries = await scoped_option_entries(page, control, policy, before)
        if entries:
            break
        await page.wait_for_timeout(150)
    if not entries or not all([await item.evaluate("el=>el.matches('.area-item-name')") for _, item in entries]):
        raise ValueError('没有读取到属于当前地区框的菜单，已停止填写')
    root = await _owned_root(page, entries[0][1])
    if len(path) > 1 and root is None:
        raise ValueError('地区层级菜单的确认入口不明确，已停止填写')
    await _watch_region_interactions(control, root)
    selected = []
    try:
        if root is not None and not best_option(path[0], [text for text, _ in entries]):
            # Reopening a picker can retain a previous city-level list.
            # Reset only through its observed, owned 全国 breadcrumb.
            homes = [node for node in await root.locator('.phoenix-breadcrumb-text').all()
                     if await node.is_visible() and (await node.inner_text()).strip() == '全国']
            if len(homes) != 1:
                raise ValueError('地区菜单不在根层级，也没有唯一的全国入口')
            if before_write:
                await before_write()
            await homes[0].click(timeout=8000)
            await page.wait_for_timeout(200)
        for index, wanted in enumerate(path):
            entries = await scoped_option_entries(page, control, policy, before)
            if root is not None:
                entries = [(text, item) for text, item in entries
                           if await item.evaluate('(el, marker)=>el.closest("[data-zhida-region-root]")?.dataset.zhidaRegionRoot===marker',
                                                  await root.get_attribute('data-zhida-region-root'))]
            match = best_option(wanted, [text for text, _ in entries])
            matches = [item for text, item in entries if text == match] if match else []
            if len(matches) != 1:
                choices = '、'.join(text for text, _ in entries[:10]) or '未读到选项'
                raise ValueError(f'地区第{index + 1}级找不到唯一的“{wanted}”；当前选项：{choices}')
            item = matches[0]
            if index < len(path) - 1:
                arrow = item.locator('svg.area-icon-right.visible')
                label = item.locator('.area-text-label')
                if await arrow.count() != 1 or not await arrow.is_visible() or await label.count() != 1:
                    raise ValueError(f'“{match}”没有明确的下一级入口，未擅自省略用户的市/区信息')
                if before_write:
                    await before_write()
                await label.click(timeout=8000)
                # A breadcrumb proves that the next list belongs to this parent.
                progressed = False
                for _ in range(8):
                    crumbs = await root.locator('.phoenix-breadcrumb-text').all_text_contents()
                    if best_option(match, [text.strip() for text in crumbs]):
                        progressed = True
                        break
                    await page.wait_for_timeout(150)
                if not progressed:
                    raise ValueError(f'未核实“{match}”的下一级层级，已停止填写')
                await page.wait_for_timeout(200)
            else:
                icon = item.locator('.icon-container.visible').filter(
                    has=page.locator('svg.area-icon-RadioUnchecked,svg.area-icon-RadioChecked'))
                if await icon.count() != 1 or not await icon.is_visible():
                    # Legacy simple area lists can select the row directly.
                    if root is not None:
                        raise ValueError('地区没有唯一的选中图标，已停止填写')
                    if before_write:
                        await before_write()
                    await item.click(timeout=8000)
                else:
                    if before_write:
                        await before_write()
                    await icon.click(timeout=8000)
                    if root is not None:
                        await page.wait_for_timeout(150)
                        # Reactive menus can replace the option node on check.
                        fresh_entries = await scoped_option_entries(page, control, policy, before)
                        checked_items = [node for text, node in fresh_entries if text == match]
                        if len(checked_items) != 1:
                            raise ValueError('地区选中后选项归属发生变化，未点击确定')
                        icon = checked_items[0].locator('.icon-container.visible')
                        if await icon.locator('svg.area-icon-RadioChecked').count() != 1:
                            raise ValueError('地区未显示选中状态，未点击确定')
                        buttons = root.locator('.phoenix-button')
                        confirms = [button for button in await buttons.all()
                                    if await button.is_visible() and (await button.inner_text()).strip() == '确定']
                        if len(confirms) != 1:
                            raise ValueError('当前地区菜单没有唯一的确定按钮，已停止填写')
                        if before_write:
                            await before_write()
                        await confirms[0].click(timeout=8000)
            selected.append(match)
        await page.wait_for_timeout(250)
        # Store only an audit of a completed owned operation. The value itself
        # must be committed, not a search input or a staged radio selection.
        await control.evaluate(r"""(el, path) => {
          const value=String(el.querySelector('.phoenix-select__content')?.innerText||'').replace(/\s+/g,' ').trim();
          const leaf=path.split('/').pop();
          if(value!==leaf&&value!==path)return;
          el.dataset.zhidaRegionCommittedPath=path;
          el.dataset.zhidaRegionCommittedValue=value;
          el.dataset.zhidaRegionCommittedEpoch=el.dataset.zhidaRegionEpoch||'0';
          el.dataset.zhidaRegionCommittedTitle=String(el.closest('.form-item--phoenix')?.querySelector('.form-item__title')?.innerText||'').trim();
        }""", '/'.join(selected))
        return ['/'.join(selected)]
    except Exception:
        # Cancel only this proven widget, not an unrelated dialog or form.
        if root is not None and await root.is_visible():
            cancels = [button for button in await root.locator('.phoenix-button').all()
                       if await button.is_visible() and (await button.inner_text()).strip() == '取消']
            if len(cancels) == 1:
                await cancels[0].click(timeout=3000)
        raise
