"""Network-isolated entry confirmation regression. No real ATS request."""
import asyncio
from playwright.async_api import async_playwright
from autohome_navigation_regression_test import BASE, TITLE, card, fixture
from app.ats_adapters import inspect_application_page, start_application
from app.autohome_confirmation import ENTRY_MESSAGE
from app.browser_models import ApplicationTarget
from app.job_navigation import workflow_fingerprint

TARGET = ApplicationTarget(company='汽车之家', job_title=TITLE, city='北京', source_url=BASE + '&pid=47900')
DIALOG_SCRIPT = r"""() => {
  window.showEntry=(pid,message)=>{
    const modal=document.createElement('div');modal.className='layui-layer-content';
    modal.innerHTML='<div class="alertdiv"><div class="sfbody"><div class="msg">'+message+'</div></div><div class="option"><div class="cancelbtn" onclick="closeClick()">取消</div><div class="btn2" onclick="yesClick()">确定</div></div></div>';
    document.body.append(modal);
    window.closeClick=()=>{window.cancelled=(window.cancelled||0)+1;modal.remove()};
    window.yesClick=()=>{modal.remove();window.open('recruit-delivery.html?pid='+pid)};
  };
  document.addEventListener('click',e=>{
    if(e.target.matches('.applybtn')) window.showEntry(window.destinationPid||e.target.getAttribute('pid'),window.entryText||'每位同学只能投递一个职位，确认要申请该职位吗？');
  });
}"""


async def reject(page, target=TARGET):
    try:
        await start_application(page, target)
    except ValueError:
        return
    raise AssertionError('unsafe entry was not rejected')


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            await context.route('**/*', lambda r: r.fulfill(body='<title>填写申请</title>', content_type='text/html'))
            page = await context.new_page()
            await page.goto(BASE)
            async def reset():
                await page.set_content(fixture(card('47900', expanded=True)))
                await page.evaluate(DIALOG_SCRIPT)
            await reset()
            before = await inspect_application_page(page, 'test', TARGET)
            await page.evaluate('(text)=>showEntry("47898",text)', ENTRY_MESSAGE)
            after = await inspect_application_page(page, 'test', TARGET)
            assert after.entry_confirmation_required
            assert workflow_fingerprint(before) != workflow_fingerprint(after)
            # Stale dialog points to another job: cancel, re-open from target.
            await start_application(page, TARGET)
            assert context.pages[-1].url.endswith('pid=47900')
            assert await page.evaluate('[window.cancelled,window.applyClicks]') == [1, 1]
            await context.pages[-1].close()
            await reset()
            wrong_target = TARGET.model_copy(update={'source_url': BASE + '&pid=47898'})
            await reject(page, wrong_target)
            assert await page.evaluate('window.applyClicks') == 0
            await reset()
            await page.evaluate('()=>showEntry("47900","您已经投递过其他职位，不能再投递")')
            await reject(page)
            assert await page.evaluate('window.applyClicks') == 0
            await reset()
            await page.evaluate('(text)=>{showEntry("47900",text);showEntry("47900",text)}', ENTRY_MESSAGE)
            await reject(page)
            await reset()
            await page.evaluate('window.destinationPid="47898"')
            await reject(page)
            assert context.pages[-1].url.endswith('pid=47898')
            await context.pages[-1].close()
            await reset()
            await start_application(page)  # No explicit target: leave dialog to user.
            assert len(context.pages) == 1
            assert (await inspect_application_page(page, 'test')).entry_confirmation_required
        finally:
            await browser.close()
    print('autohome_confirmation_test: OK (fresh target binding, stale/duplicate/wrong dialogs, popup pid, no submit)')


if __name__ == '__main__':
    asyncio.run(run())
