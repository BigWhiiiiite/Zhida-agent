"""Offline upload/parse evidence and exact-dialog controls; no real uploads."""
from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from app.autohome_attachment import (inspect_autohome_attachment,
    confirm_autohome_resume_parse, cancel_autohome_resume_parse)


BASE = 'https://talent.autohome.com.cn/recruit-delivery.html?pid=47900'
HTML = '''<html><head><meta charset="utf-8"></head><body>
 <div class="delivery_card"><div class="jlcard"><div class="jlbox" id="jlbox">
  <div class="uploadbox"><i id="fileicon">PDF</i>
   <a id="filename" href="https://files.example.test/resume.pdf?private=signed-fixture">示例的简历.pdf</a>
   <div class="input-box"><input type="file" id="uploadFile"><label id="uploadFileLabel">更新简历</label></div>
   <div id="file-err-msg"></div></div></div></div></div>
 <input id="profile-name" value="原资料">
 <script>window.confirmCalls=0;window.cancelCalls=0;
 function yesClick(){window.confirmCalls++;document.getElementById('profile-name').value='解析资料';document.getElementById('LAY_layuipro2').remove();}
 function closeClick(){window.cancelCalls++;document.getElementById('LAY_layuipro2').remove();}
 </script></body></html>'''
PROMPT = '''<div class="layui-layer-content" id="LAY_layuipro2"><div class="alertdiv"><div class="sfbody">
 <div class="msg">是否变更个人信息？</div><div class="option"><div class="cancelbtn" onclick="closeClick()">取消</div>
 <div class="btn2" onclick="yesClick()">确定</div></div></div></div></div>'''


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            requests=[]
            async def serve(route):
                requests.append(route.request.method)
                await route.fulfill(body=HTML,content_type='text/html; charset=utf-8')
            await context.route('**/*',serve)
            page = await context.new_page()
            await page.goto(BASE)
            before = await page.content()
            state = await inspect_autohome_attachment(page)
            assert await page.content() == before
            assert state['state']=='attachment_present' and state['selected_file_count']==0
            assert not state['upload_verified'] and 'signed-fixture' not in str(state)
            baseline = state
            await page.locator('#filename').evaluate("el=>el.href='https://files.example.test/new.pdf'")
            assert (await inspect_autohome_attachment(page,baseline))['upload_verified']
            await page.locator('body').evaluate('(el,html)=>el.insertAdjacentHTML("beforeend",html)',PROMPT)
            state = await inspect_autohome_attachment(page)
            assert state['state']=='parse_confirmation' and state['upload_verified']
            result = await confirm_autohome_resume_parse(page)
            assert result['requires_field_verification'] and result['parse_confirmation_closed']
            assert await page.locator('#profile-name').input_value()=='解析资料'
            assert await page.evaluate('window.confirmCalls')==1

            await page.goto(BASE)
            await page.locator('body').evaluate('(el,html)=>el.insertAdjacentHTML("beforeend",html)',PROMPT)
            result=await cancel_autohome_resume_parse(page)
            assert result['parse_confirmation_cancelled'] and not result['requires_field_verification']
            assert await page.locator('#profile-name').input_value()=='原资料'
            assert await page.evaluate('[window.confirmCalls,window.cancelCalls]')==[0,1]

            # A stale attachment must not turn a new upload failure into success.
            await page.locator('#file-err-msg').evaluate("el=>el.textContent='上传文件超时'")
            state=await inspect_autohome_attachment(page)
            assert state['state']=='upload_error' and not state['upload_verified']
            for message in ('确认提交简历？','每位同学只能投递一个职位，确认要申请该职位吗？'):
                await page.goto(BASE)
                await page.locator('body').evaluate('(el,html)=>el.insertAdjacentHTML("beforeend",html)',PROMPT.replace('是否变更个人信息？',message))
                try:
                    await confirm_autohome_resume_parse(page)
                    raise AssertionError('unrelated dialog clicked')
                except ValueError:
                    pass
                assert await page.evaluate('window.confirmCalls')==0
            await page.goto(BASE)
            await page.locator('#filename').evaluate("el=>el.style.display='none'")
            assert not (await inspect_autohome_attachment(page))['attachment_present']
            await page.goto('https://example.test/recruit-delivery.html')
            assert not (await inspect_autohome_attachment(page))['supported']
            assert requests and all(method=='GET' for method in requests)
        finally:
            await browser.close()
    print('autohome_attachment_test: OK (cleared input, attachment evidence, errors, exact confirm/cancel, no submissions)')


if __name__=='__main__':
    asyncio.run(run())
