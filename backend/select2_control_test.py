"""Select2 canonicalization, scoped portal writes and readback; offline only."""
from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from app.browser_service import BrowserDemoService


HTML = '''<!doctype html><html><head><title>Select2 isolated fixture</title><style>
 .form-field{margin:20px}.select2-container{display:inline-block}
 .select2-selection{display:block;padding:12px;border:1px solid;width:200px}
 .select2-hidden-accessible{clip:rect(0 0 0 0);clip-path:inset(50%);height:1px;
   width:1px;overflow:hidden;position:absolute;padding:0;border:0}
 .select2-results{display:block;margin:12px;border:1px solid}
 .select2-results__option{padding:8px;cursor:pointer}
</style></head><body>
 <div class="form-field"><label for="city-control">期望工作城市</label>
   <select id="city" class="select2-hidden-accessible" tabindex="-1" aria-hidden="true" required>
     <option value="" disabled selected>请选择</option><option value="beijing">北京</option><option value="shanghai">上海</option>
   </select><span class="select2 select2-container"><span class="selection">
     <span id="city-control" class="select2-selection select2-selection--single" role="combobox"
       aria-haspopup="true" aria-expanded="false" aria-controls="city-results" tabindex="0">
       <span class="select2-selection__rendered">请选择</span>
     </span></span><span class="dropdown-wrapper" aria-hidden="true"></span></span>
 </div>
 <div class="form-field"><label for="other-control">其他地点</label>
   <select id="other" class="select2-hidden-accessible" tabindex="-1" aria-hidden="true" required>
     <option value="" disabled selected>请选择</option><option value="other-beijing">北京</option><option value="shenzhen">深圳</option>
   </select><span class="select2 select2-container"><span class="selection">
     <span id="other-control" class="select2-selection select2-selection--single" role="combobox"
       aria-haspopup="true" aria-expanded="false" aria-controls="other-results" tabindex="0">
       <span class="select2-selection__rendered">请选择</span>
     </span></span><span class="dropdown-wrapper" aria-hidden="true"></span></span>
 </div>
 <script>
 window.selectionEvents=[];
 for(const id of ['city','other']){
   const native=document.getElementById(id),control=document.getElementById(id+'-control');
   control.addEventListener('click',()=>{
     if(document.getElementById(id+'-results')) return;
     const portal=document.createElement('span');portal.className='select2-results';
     const tree=document.createElement('ul');tree.id=id+'-results';tree.setAttribute('role','tree');
     for(const option of native.options){
       if(option.disabled) continue;
       const entry=document.createElement('li');entry.className='select2-results__option';
       entry.setAttribute('role','treeitem');entry.setAttribute('aria-selected','false');entry.textContent=option.text;
       entry.addEventListener('click',()=>{
         native.value=option.value;native.dispatchEvent(new Event('change',{bubbles:true}));
         control.querySelector('.select2-selection__rendered').textContent=option.text;
         window.selectionEvents.push({control:id,label:option.text,value:option.value});
         control.setAttribute('aria-expanded','false');portal.remove();
       });tree.append(entry);
     }
     portal.append(tree);document.body.append(portal);control.setAttribute('aria-expanded','true');
   });
 }
 document.addEventListener('keydown',event=>{
   if(event.key==='Escape'){
     for(const portal of document.querySelectorAll('.select2-results')) portal.remove();
     for(const control of document.querySelectorAll('[role="combobox"]')) control.setAttribute('aria-expanded','false');
   }
 });
 </script></body></html>'''


async def run() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            requests = []
            async def serve(route):
                requests.append((route.request.method, route.request.url))
                await route.fulfill(body=HTML, content_type='text/html; charset=utf-8')
            await context.route('**/*', serve)
            page = await context.new_page()
            await page.goto('https://select2.example.test/application')
            service = BrowserDemoService()
            service.page, service.context, service.session_id = page, context, 'select2-fixture'
            snapshot = await service.snapshot()
            assert len(snapshot.fields) == 2, [f.model_dump() for f in snapshot.fields]
            assert all(f.field_type == 'combobox' and f.required for f in snapshot.fields)
            controls = {await page.locator(f.selector).get_attribute('id'): f for f in snapshot.fields}
            city, other = controls['city-control'], controls['other-control']
            assert city.options == ['北京', '上海'] and other.options == ['北京', '深圳']
            assert city.current_value == other.current_value == ''
            assert await page.evaluate('window.selectionEvents') == []

            # A separate field's already-open portal contains the same 北京.
            # Only the target's own portal may be used, even with both visible.
            await page.locator('#other-control').click()
            assert await service._select_custom(city, ['北京']) == ['北京']
            assert await service._read_field_value(city) == '北京'
            assert await page.locator('#city').input_value() == 'beijing'
            assert await page.locator('#other').input_value() == ''
            assert await page.evaluate('window.selectionEvents') == [
                {'control': 'city', 'label': '北京', 'value': 'beijing'}]
            assert await page.locator('#other-results').count() == 1

            # Refreshing the snapshot still reports one field and the actual
            # selected value, then the other control remains independently writable.
            refreshed = await service.snapshot()
            assert len(refreshed.fields) == 2
            new_city = next(f for f in refreshed.fields if f.selector == city.selector)
            assert new_city.current_value == '北京'
            assert await service._select_custom(other, ['深圳']) == ['深圳']
            assert await service._read_field_value(other) == '深圳'
            assert await service._read_field_value(city) == '北京'
            assert await page.locator('#other').input_value() == 'shenzhen'
            assert await page.evaluate('window.selectionEvents.map(e=>e.control)') == ['city', 'other']
            assert all(method == 'GET' for method, _ in requests), requests
        finally:
            await browser.close()
    print('select2_control_test: OK (native/wrapper dedup, tree portals, duplicate captions scoped, write/readback)')


if __name__ == '__main__':
    asyncio.run(run())
