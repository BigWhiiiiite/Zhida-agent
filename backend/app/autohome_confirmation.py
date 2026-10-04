"""Bounded AutoHome entry navigation, not an application submission.

Official school-recruit-list.js opens recruit-delivery.html in the callback
of this exact confirmation. Never reuse a leftover callback's unknown pid.
"""
from urllib.parse import parse_qs, urlparse

from playwright.async_api import Page

from .browser_models import ApplicationTarget


ENTRY_MESSAGE = "每位同学只能投递一个职位，确认要申请该职位吗？"


async def entry_dialog(page: Page):
    parsed = urlparse(page.url)
    if parsed.scheme != "https" or parsed.hostname != "talent.autohome.com.cn" or parsed.path != "/campus-recruit-list.html":
        return None
    dialogs = page.locator('.layui-layer-content .alertdiv')
    visible = [dialogs.nth(i) for i in range(await dialogs.count()) if await dialogs.nth(i).is_visible()]
    if not visible:
        return None
    if len(visible) != 1:
        raise ValueError("汽车之家出现多个确认窗口，请人工核对")
    dialog = visible[0]
    message = dialog.locator('.sfbody .msg')
    if await message.count() != 1 or ''.join((await message.inner_text()).split()) != ENTRY_MESSAGE:
        raise ValueError("汽车之家出现非预期提示，可能涉及已投递或岗位限制，请人工核对")
    return dialog


async def has_entry_confirmation(page: Page) -> bool:
    try:
        return await entry_dialog(page) is not None
    except ValueError:
        return False


async def enter_selected_application(page: Page, job_id: str, job_title: str,
                                     target: ApplicationTarget) -> None:
    """Only a freshly opened, target-bound entry confirmation may be accepted."""
    parsed = urlparse(target.source_url)
    target_ids = parse_qs(parsed.query).get('pid', [])
    target_bound = (parsed.scheme == 'https' and parsed.hostname == 'talent.autohome.com.cn'
                    and parsed.path in {'/campus-recruit-list.html', '/recruit-delivery.html'}
                    and target_ids == [job_id] and target.job_title == job_title)
    if target.source_url and not target_bound:
        raise ValueError("汽车之家展开岗位与任务绑定的岗位不一致，请重新选择")
    # A dialog has no pid in its DOM. Cancel a known leftover before reopening
    # from the verified card; never confirm a stale callback for another job.
    old = await entry_dialog(page)
    if old is not None:
        if not target_bound:
            raise ValueError("请先绑定准确的汽车之家岗位，再处理进入申请页的确认")
        cancel = old.locator('.option > .cancelbtn[onclick="closeClick()"]')
        if await cancel.count() != 1 or not await cancel.is_visible() or (await cancel.inner_text()).strip() != '取消':
            raise ValueError("汽车之家确认窗口结构已变化，请人工核对")
        await cancel.click(timeout=4000)
        await old.wait_for(state='hidden', timeout=4000)
    entry = page.locator(f'.position_card_li[pid="{job_id}"] .overview .applybtn[pid="{job_id}"]')
    if await entry.count() != 1 or not await entry.is_visible() or (await entry.inner_text()).strip() != '申请该职位':
        raise ValueError("汽车之家岗位申请入口已变化，请重新核对")
    await entry.click(timeout=4000)
    await page.wait_for_timeout(1500)
    dialog = await entry_dialog(page)
    if dialog is None or not target_bound:
        # Login navigation, or no explicit target: do not infer authorization.
        return
    confirm = dialog.locator('.option > .btn2[onclick="yesClick()"]')
    if await confirm.count() != 1 or not await confirm.is_visible() or (await confirm.inner_text()).strip() != '确定':
        raise ValueError("汽车之家确认按钮结构已变化，请人工核对")
    async with page.expect_popup(timeout=12000) as popup_info:
        await confirm.click(timeout=4000)
    popup = await popup_info.value
    await popup.wait_for_url(lambda url: str(url) != 'about:blank', timeout=15000)
    await popup.wait_for_load_state('domcontentloaded', timeout=30000)
    destination = urlparse(popup.url)
    if (destination.scheme != 'https' or destination.hostname != 'talent.autohome.com.cn'
            or destination.path != '/recruit-delivery.html'
            or parse_qs(destination.query).get('pid', []) != [job_id]):
        raise ValueError("申请页与选定的汽车之家岗位不一致，已停止，不会填写个人资料")
    await popup.wait_for_timeout(1500)
