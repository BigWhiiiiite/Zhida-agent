"""Offline regression for exact, single-record Autohome section expansion."""
import asyncio

from playwright.async_api import async_playwright

from app.autohome_sections import discover_autohome_sections, expand_autohome_section


HTML = '''<html><head><meta charset="utf-8"></head><body><form class="validform"></form><script>
window.calls=[];
const configs=[['教育经历','eduTemplate','addEdu','School',1],
 ['实习/工作经历','workTemplate','addWork','Company',0],
 ['项目经历','projectTemplate','addProject','ProjectName',1]];
for(const [title,template,handler,anchor,initial] of configs){
 const card=document.createElement('div');card.className='icard';
 card.innerHTML=`<div class="title">${title}</div><div class="content"><div class="contentFirstRow">
   <div class="addbtn" data-bind="click: ${handler}">增加${title}</div></div>
   <div data-bind="template:{name:'${template}',foreach:records}"></div></div>`;
 document.querySelector('form').append(card);
 const root=card.querySelector('[data-bind*="template:"]');
 const add=()=>{
   root.insertAdjacentHTML('beforeend',`<div class="numtxt">${title}</div><div class="row"><div class="col">
    <div class="labeltag"><div class="label">名称</div></div><div class="control"><input data-bind="value: ${anchor}"></div>
   </div></div><div class="option"><div class="del" data-bind="click: remove">删除${title}</div>
    <div class="add" data-bind="click: $parent.${handler}">增加${title}</div></div><div class="splitline"></div>`);
   card.querySelector('.addbtn').style.display='none';
 };
 for(let i=0;i<initial;i++)add();
 card.addEventListener('click',event=>{
   if(event.target.matches('.add,.addbtn')){window.calls.push(handler);add();}
   if(event.target.matches('.del'))window.calls.push('DELETE');
 });
}
</script><div class="add" data-bind="click: addEdu">增加教育经历</div>
<div class="save">提交简历</div></body></html>'''


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            await context.route('**/*', lambda r: r.fulfill(body=HTML,content_type='text/html'))
            page = await context.new_page()
            await page.goto('https://talent.autohome.com.cn/recruit-delivery.html?pid=47900')
            before = await page.content()
            candidates = await discover_autohome_sections(page)
            assert await page.content() == before
            assert len(candidates) == 3, candidates
            assert [r['record_count'] for r in candidates] == [1, 0, 1]
            for kind in ('education', 'experience', 'project'):
                chosen = next(r for r in await discover_autohome_sections(page) if r['semantic_section'] == kind)
                result = await expand_autohome_section(page, chosen['id'])
                assert result['record_count'] == chosen['record_count'] + 1 and result['added_count'] == 1
                assert len(set(result['record_keys'])) == result['record_count']
                try:
                    await expand_autohome_section(page, chosen['id'])
                    raise AssertionError('stale candidate clicked twice')
                except ValueError:
                    pass
            assert await page.evaluate('window.calls') == ['addEdu', 'addWork', 'addProject']
            assert [r['record_count'] for r in await discover_autohome_sections(page)] == [2, 1, 2]
            # Exact title, handler and shape are all mandatory.
            await page.locator('.icard').first.locator('.add').evaluate_all("nodes=>nodes.forEach(n=>n.setAttribute('data-bind','click: submit'))")
            assert not any(r['semantic_section']=='education' for r in await discover_autohome_sections(page))
            # No-op handler is clicked only once, never repeated automatically.
            await page.locator('.icard').nth(2).evaluate("el=>el.addEventListener('click',e=>e.stopImmediatePropagation(),true)")
            project = next(r for r in await discover_autohome_sections(page) if r['semantic_section']=='project')
            try:
                await expand_autohome_section(page, project['id'])
                raise AssertionError('no-op accepted')
            except ValueError:
                pass
            assert await page.evaluate('window.calls') == ['addEdu', 'addWork', 'addProject']
            await page.goto('https://example.test/recruit-delivery.html')
            assert await discover_autohome_sections(page) == []
        finally:
            await browser.close()
    print('autohome_sections_test: OK (exact sections, one click/record, isolation, stale/unsafe/no-op guards)')


if __name__ == '__main__':
    asyncio.run(run())
