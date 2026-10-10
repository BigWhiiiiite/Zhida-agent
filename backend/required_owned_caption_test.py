"""Anonymous read-only regressions for required marks outside Ant inner rows."""
import asyncio

from playwright.async_api import async_playwright

from app.browser_service import BrowserDemoService
from app.form_observation import model_page


def question(caption, control, caption_class="", extra=""):
    return ('<div class="resume-form-item"><div class="resume-form-title ' + caption_class + '">' +
            caption + '</div><div class="ant-row ant-form-item"><div class="ant-form-item-control">' +
            control + '</div></div>' + extra + '</div>')


HTML = '''<meta charset="utf-8"><style>
.own-required::before { content: "*"; }
.own-required-after::after { content: "*"; }
.unrelated-star::before { content: "*"; }
</style><main><h1>匿名表单</h1>''' + ''.join([
    question('民族', '<input aria-label="民族" value="fixture-only">', 'own-required'),
    question('实习岗位<span class="required">*</span>', '<input>'),
    question('年龄', '<input type="number">', 'own-required'),
    question('身高', '<input type="number">', 'own-required-after'),
    question('出生日期', '<div class="ant-picker"><div class="ant-picker-input">'
             '<input readonly></div></div>', 'own-required'),
    question('性别', '<label><input type="radio" name="gender" value="male">男</label>'
             '<label><input type="radio" name="gender" value="female">女</label>', 'own-required'),
    question('选填备注', '<input>', '', '<small class="unrelated-star">邻近提示不是该题必填证据</small>'),
    question('选填说明<span class="required" style="display:none">*</span>', '<input>'),
    '<div class="shared"><div class="resume-form-title own-required">共同题名</div><input><input></div>',
]) + '''<button type="submit">提交</button></main><script>
window.clicks=0;window.changes=0;window.inputs=0;window.submits=0;
document.addEventListener('click',()=>window.clicks++);
document.addEventListener('change',()=>window.changes++);
document.addEventListener('input',()=>window.inputs++);
document.addEventListener('submit',e=>{window.submits++;e.preventDefault()});
</script>'''


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            await context.route('**/*', lambda route: route.abort())
            await context.route_web_socket('**/*', lambda socket: socket.close())
            page = await context.new_page()
            await page.set_content(HTML)
            service = BrowserDemoService()
            service.page, service.context, service.session_id = page, context, 'required-fixture'
            snapshot = await service.snapshot(probe_options=False)
            for caption in ('民族', '年龄', '身高', '出生日期', '性别'):
                members = [f for f in snapshot.fields if f.question_text == caption]
                assert members and all(f.required and f.required_evidence for f in members), caption
                assert all(f.observation.required_status == 'required' for f in members)
            role = next(f for f in snapshot.fields if '实习岗位' in f.question_text)
            assert role.required and role.required_evidence
            note = next(f for f in snapshot.fields if f.question_text == '选填备注')
            assert not note.required and not note.required_evidence
            hidden_mark = next(f for f in snapshot.fields if '选填说明' in f.question_text)
            assert not hidden_mark.required and not hidden_mark.required_evidence
            shared = [f for f in snapshot.fields if '共同题名' in f.question_text]
            assert len(shared) == 2 and not any(f.required for f in shared)
            for item in model_page(snapshot)['questions']:
                if item['question_text'] in {'民族', '年龄', '身高', '出生日期', '性别'}:
                    assert item['required'] and item['required_evidence']
            assert await page.locator('input[aria-label="民族"]').input_value() == 'fixture-only'
            assert await page.evaluate('({clicks, changes, inputs, submits})') == {
                'clicks': 0, 'changes': 0, 'inputs': 0, 'submits': 0}
        finally:
            await browser.close()
    print('required_owned_caption_test: OK (owned text/choice/calendar marks, no adjacent-star inheritance or value writes)')


if __name__ == '__main__':
    asyncio.run(run())
