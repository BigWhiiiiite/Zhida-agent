"""Batch page identity regressions with a fresh, fully offline browser.

All documents are anonymous fixtures fulfilled locally. No dotenv, API, DB,
existing browser profile, real recruitment site, service worker or socket.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from playwright.async_api import async_playwright

from app.browser_models import ExecutePlanRequest, FillAction
from app.browser_service import BrowserDemoService


URL = "https://execution.example.test/application?jobId=offline-one"
FORM = """<!doctype html><title>Offline application fixture</title>
<main><h1>你正在投递职位：匿名系统研发工程师</h1><h2>个人信息</h2>
<label for="name">姓名</label><input id="name" name="name">
<label for="email">邮箱</label><span id="email-caption" hidden>邮箱</span>
<input id="email" name="email" aria-labelledby="email-caption">
<h2>教育经历</h2><label for="school">学校</label><input id="school" name="school">
<label for="city">城市<select id="city" name="city"><option value="">请选择</option></select></label>
<button type="button">下一步</button></main>
<script>
window.emailWrites=[];
document.querySelector('#email').addEventListener('input',event=>window.emailWrites.push(event.target.value));
</script>"""
TRANSITIONS = {
    "url_change": "history.replaceState({},'', '/application?jobId=offline-two')",
    "login_expired": "document.body.insertAdjacentHTML('beforeend', '<div role=dialog aria-modal=true><h2>登录</h2><input type=password><button>登录</button></div>')",
    "job_changed_same_url": "document.querySelector('h1').textContent='你正在投递职位：匿名财务分析工程师'",
    "step_changed_same_url": "document.querySelector('main').insertAdjacentHTML('afterbegin','<p>第 2 步 共 3 步</p>')",
    "native_label_reused": "document.querySelector('label[for=email]').textContent='紧急联系人邮箱'",
    "native_aria_caption_reused": "document.querySelector('#email-caption').textContent='紧急联系人邮箱'",
    "native_name_reused": "document.querySelector('#email').name='emergency_email'",
    "native_type_reused": "document.querySelector('#email').type='tel'",
    "native_record_reused": "const section=document.createElement('section');section.innerHTML='<h2>紧急联系人</h2>';section.append(document.querySelector('#email'));document.querySelector('main').append(section)",
}


class ReloadAfterFirstRead(BrowserDemoService):
    reloaded = False

    async def _read_field_value(self, field):
        value = await super()._read_field_value(field)
        if field.name == "name" and not self.reloaded:
            self.reloaded = True
            await self.page.reload(wait_until="domcontentloaded")
        return value


async def run() -> None:
    problems = []
    with patch("dotenv.load_dotenv", side_effect=AssertionError("dotenv forbidden")), \
         patch("sqlite3.connect", side_effect=AssertionError("database forbidden")):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                context = await browser.new_context(service_workers="block")
                requests = []

                async def serve(route):
                    requests.append((route.request.method, route.request.url))
                    if route.request.method == "GET" and route.request.url == URL:
                        await route.fulfill(body=FORM, content_type="text/html; charset=utf-8")
                    else:
                        await route.abort()

                await context.route("**/*", serve)
                await context.route_web_socket("**/*", lambda socket: socket.close())
                for mode in [*TRANSITIONS, "same_url_reload", "reactive_options"]:
                    page = await context.new_page()
                    await page.goto(URL)
                    service = ReloadAfterFirstRead() if mode == "same_url_reload" else BrowserDemoService()
                    service.page, service.context, service.session_id = page, context, "transition-fixture"
                    state = await service.workflow_state(service.session_id)
                    assert state.stage == "application_form", state.model_dump()
                    snapshot = await service.snapshot()
                    fields = {field.name: field for field in snapshot.fields if field.name}
                    city = fields["city"]
                    if mode in TRANSITIONS:
                        await page.locator("#name").evaluate(
                            "(el, body) => el.addEventListener('input',new Function(body),{once:true})", TRANSITIONS[mode])
                    elif mode == "reactive_options":
                        await page.locator("#name").evaluate("""el => el.addEventListener('input',()=>{
                          document.querySelector('#city').innerHTML='<option value="">请选择</option><option value="bj">北京</option>';
                          document.querySelector('main').insertAdjacentHTML('beforeend','<label>专业<input name="major"></label>');
                        },{once:true})""")
                    actions = [FillAction(selector=fields["name"].selector, label="姓名", action="fill", value="合成候选人", confidence=1),
                               FillAction(selector=fields["email"].selector, label="邮箱", action="fill", value="candidate@example.test", confidence=1)]
                    if mode == "reactive_options":
                        actions.append(FillAction(selector=city.selector, label="城市", action="select", value="北京", confidence=1))
                    error = ""
                    result = None
                    try:
                        result = await service.execute(service.session_id, ExecutePlanRequest(actions=actions))
                    except (ValueError, LookupError) as exc:
                        error = str(exc)
                    email = await page.locator("#email").input_value()
                    writes = await page.evaluate("window.emailWrites")
                    if mode == "reactive_options":
                        assert result and result.completed == result.verified == 3, (error, result)
                        assert await page.locator("#city").input_value() == "bj"
                        assert writes == ["candidate@example.test"]
                    else:
                        print(f"{mode}: email={email!r}, writes={writes}, stopped={bool(error) or bool(result and result.failed)}")
                        if email or writes:
                            problems.append(f"{mode}: old batch wrote email after page transition")
                    await page.close()
                assert requests and all(method == "GET" and url == URL for method, url in requests), requests
            finally:
                await browser.close()
    assert not problems, "; ".join(problems)
    print("execute_page_transition_test: OK (URL/auth/job/step/document and native question ownership changes blocked; reactive options continue)")


if __name__ == "__main__":
    asyncio.run(run())
