"""Network-isolated, value-free coverage of unread Shadow/folded surfaces."""
import asyncio
import json

from playwright.async_api import async_playwright

from app.browser_service import BrowserDemoService
from app.extraction_audit import audit_page


BASE = '<meta charset="utf-8"><label>姓名<input></label>'
EVENTS = '''<script>window.operations=0;for(const name of ['click','input','change','submit'])
document.addEventListener(name,()=>window.operations++);</script>'''


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            await context.route('**/*', lambda route: route.abort())
            await context.route_web_socket('**/*', lambda socket: socket.close())
            page = await context.new_page()
            service = BrowserDemoService()
            service.page, service.context, service.session_id = page, context, 'anonymous-surfaces'

            async def observe(html):
                await page.set_content(BASE + html + EVENTS)
                snapshot = await service.snapshot(probe_options=False)
                payload = audit_page(snapshot)
                assert await page.evaluate('window.operations') == 0
                assert payload['input_manifest']['all_collected_questions_included']
                assert not payload['input_manifest']['hidden_or_future_questions_covered']
                return payload

            # The outer root only contains another host. The inner root's
            # controls must be detected without querying their labels/values.
            for style in ('', 'style="display:contents"'):
                nested = await observe('<div id="outer" ' + style + '''></div><script>(()=>{
const first=document.getElementById('outer').attachShadow({mode:'open'});
first.innerHTML='<div id="inner"></div>';
const second=first.querySelector('#inner').attachShadow({mode:'open'});
second.innerHTML='<label>shadow-private-caption<input value="shadow-private-value"></label>';
})();</script>''')
                assert nested['extraction_report']['unread_shadow_regions'] == 1
                assert nested['extraction_report']['capture_status'] == 'partial'
                assert [q['question_text'] for q in nested['questions']] == ['姓名']
                assert 'shadow-private' not in json.dumps(nested)
            # A non-form shadow surface is not automatically a missing form.
            plain = await observe('''<div id="host"></div><script>
document.getElementById('host').attachShadow({mode:'open'}).innerHTML='<p>Decorative only</p>';
</script>''')
            assert plain['extraction_report']['unread_shadow_regions'] == 0
            # A hidden host is outside the current rendered-document scope.
            hidden = await observe('''<div id="host" hidden></div><script>
document.getElementById('host').attachShadow({mode:'open'}).innerHTML='<input value="hidden-shadow-value">';
</script>''')
            assert hidden['extraction_report']['unread_shadow_regions'] == 0

            folded = await observe('''<div class="resume-tpl-wrap"><div class="section-title">教育经历</div>
<div style="display:none"><div class="resume-form-wrap">
<label>学校名称<input value="hidden-school-value"></label>
<label>学院名称<input value="hidden-college-value"></label>
<label>专业名称<input value="hidden-major-value"></label>
</div></div></div>''')
            assert folded['extraction_report']['pending_sections'] == ['教育经历（已发现未呈现控件）']
            assert folded['extraction_report']['capture_status'] == 'partial'
            assert [q['question_text'] for q in folded['questions']] == ['姓名']
            assert 'hidden-' not in json.dumps(folded)
            # The observed Ant boundary owns a direct caption even when its
            # title uses a custom DIV class rather than a semantic heading.
            ant_caption = await observe('<div class="resume-tpl-wrap"><div class="resume-tpl-caption">教育经历</div>'
                                        '<div hidden><input></div></div>')
            assert ant_caption['extraction_report']['pending_sections'] == ['教育经历（已发现未呈现控件）']
            # Native section/legend boundaries carry their own caption too.
            native = await observe('<fieldset><legend>项目经历</legend><div hidden><input></div></fieldset>')
            assert native['extraction_report']['pending_sections'] == ['项目经历（已发现未呈现控件）']
            # Empty known record section + its own explicit Add entry is an
            # observed unpresented slot, never permission to click Add.
            empty = await observe('<section><h2>实习经历</h2><button>添加经历</button></section>')
            assert empty['extraction_report']['pending_sections'] == ['实习经历（有新增入口，记录尚未呈现）']
            ordinary_empty = await observe('<section><h2>实习经历</h2><p>暂无记录</p></section>')
            assert not ordinary_empty['extraction_report']['pending_sections']
            existing = await observe('<section><h2>实习经历</h2><label>实习单位<input></label><button>添加经历</button></section>')
            assert not existing['extraction_report']['pending_sections']
            # File inputs intentionally hidden behind a visible upload UI are
            # already captured; do not invent an unread folded section.
            attachment = await observe('<section><h2>附件</h2><label for="upload">上传附件</label>'
                                       '<input id="upload" type="file" style="display:none"></section>')
            assert not attachment['extraction_report']['pending_sections']
            assert any(q['controls'][0]['field_type'] == 'file' for q in attachment['questions'])
            # A nearby/ambiguous/unknown title cannot be promoted to an owner.
            for html in ('<h2>教育经历</h2><div class="resume-tpl-wrap"><div hidden><input></div></div>',
                         '<section><h2>教育经历</h2><h2>项目经历</h2><div hidden><input></div></section>',
                         '<section><h2>hidden-private-heading</h2><div hidden><input></div></section>'):
                rejected = await observe(html)
                assert not rejected['extraction_report']['pending_sections']
                assert 'hidden-private-heading' not in json.dumps(rejected)
            # Parent titles must not borrow a nested section's hidden controls.
            nested_section = await observe('<section><h2>教育经历</h2><section><h2>项目经历</h2>'
                '<div hidden><input></div></section></section>')
            assert nested_section['extraction_report']['pending_sections'] == ['项目经历（已发现未呈现控件）']
            iframe = await observe('<iframe srcdoc="&lt;label&gt;private-frame-caption&lt;input&gt;&lt;/label&gt;"></iframe>')
            assert iframe['extraction_report']['embedded_regions'] == 1
            assert iframe['extraction_report']['capture_status'] == 'partial'
            assert [q['question_text'] for q in iframe['questions']] == ['姓名']
        finally:
            await browser.close()
    print('extraction_surface_inventory_test: OK (nested open-shadow and own folded regions marked partial, no hidden values/clicks/writes)')


if __name__ == '__main__':
    asyncio.run(run())
