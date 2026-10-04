"""Full BrowserDemoService import regressions on intercepted anonymous pages."""
from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from app.browser_service import BrowserDemoService


BASE = 'https://talent.autohome.com.cn/recruit-delivery.html?pid=47900'
HTML = '''<!doctype html><html><head><meta charset="utf-8"><title>匿名上传回归</title></head><body>
 <div class="delivery_card"><div class="title"><span class="requiretag">*</span><span>简历附件</span></div>
  <div class="jlcard"><div class="jlbox2" id="jlbox"><div class="uploadbox">
   <i id="fileicon" style="display:none">PDF</i><a id="filename" style="display:none"></a>
   <div class="input-box"><input type="file" id="uploadFile" name="inputFile">
    <label for="inputFlie" id="uploadFileLabel">点击上传</label></div><div id="file-err-msg"></div>
  </div></div></div></div>
 <div class="delivery_card"><div class="title">完善个人信息</div><div class="icards"><form class="validform">
  <div class="icard"><div class="title">基本信息</div><div class="content"><div class="row">
   <div class="col"><div class="labeltag"><div class="requireTag">*</div><div class="label">姓名</div></div>
    <div class="control"><input id="profile-name" placeholder="请输入姓名" datatype="*" value="原姓名"></div></div>
   <div class="col"><div class="labeltag"><div class="requireTag">*</div><div class="label">邮箱</div></div>
    <div class="control"><input id="profile-email" placeholder="请输入邮箱" datatype="e" value="before@example.test"></div></div>
  </div></div></div></form></div>
  <div class="scard fixbottom"><div class="save" onclick="window.finalSubmitCalls++;fetch('/forbidden-submit',{method:'POST'})">提交简历</div></div>
 </div>
 <script>
 window.mode='success';window.fileChanges=0;window.parseCalls=0;window.cancelCalls=0;window.finalSubmitCalls=0;
 function attachment(version){
   const box=document.getElementById('jlbox');box.className='jlbox';
   document.getElementById('fileicon').style.display='';
   const link=document.getElementById('filename');link.href='https://files.example.test/'+version+'.pdf';
   link.textContent='示例的简历.pdf';link.style.display='';
   document.getElementById('uploadFileLabel').textContent='更新简历';
 }
 function prompt(message){
   document.body.insertAdjacentHTML('beforeend',`<div class="layui-layer-content" id="LAY_layuipro2">
    <div class="alertdiv"><div class="sfbody"><div class="msg">${message}</div><div class="option">
     <div class="cancelbtn" onclick="closeClick()">取消</div><div class="btn2" onclick="yesClick()">确定</div>
    </div></div></div></div>`);
 }
 function yesClick(){
   if(document.querySelector('#LAY_layuipro2 .msg').textContent!=='是否变更个人信息？'){
     window.finalSubmitCalls++;fetch('/forbidden-submit',{method:'POST'});return;
   }
   window.parseCalls++;document.getElementById('profile-name').value='解析姓名';
   document.getElementById('profile-email').value='parsed@example.test';
   document.getElementById('LAY_layuipro2').remove();
 }
 function closeClick(){window.cancelCalls++;document.getElementById('LAY_layuipro2').remove();}
 document.getElementById('uploadFile').addEventListener('change',event=>{
   window.fileChanges++;document.getElementById('file-err-msg').textContent='';
   // This is the exact relevant server-success behaviour: the input is reset
   // immediately, even though server attachment evidence survives elsewhere.
   event.target.value='';
   if(window.mode==='old')return;
   if(window.mode==='error'){document.getElementById('file-err-msg').textContent='上传失败';return;}
   const finish=()=>{attachment('new');prompt(window.mode==='wrong-dialog'?'确认提交简历？':'是否变更个人信息？');};
   if(window.mode==='async-success')setTimeout(finish,20);else finish();
 });
 </script></body></html>'''


async def run() -> None:
    with TemporaryDirectory(prefix='zhida-native-import-') as temporary:
        resume = Path(temporary) / 'fixture-resume.pdf'
        resume.write_bytes(b'%PDF-1.4\n% Synthetic upload payload only; no personal data.\n%%EOF\n')
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel='chrome', headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                requests = []
                async def serve(route):
                    requests.append((route.request.method,route.request.url))
                    await route.fulfill(body=HTML,content_type='text/html; charset=utf-8')
                await context.route('**/*',serve)
                page = await context.new_page()
                service = BrowserDemoService()
                service.page,service.context,service.session_id=page,context,'native-import-fixture'
                real_wait = page.wait_for_timeout
                async def quick_wait(_):
                    await real_wait(5)
                async def reset(mode='success', old=False):
                    await page.goto(BASE)
                    await page.evaluate('(mode)=>window.mode=mode',mode)
                    if old:
                        await page.evaluate("attachment('old')")
                async def counters():
                    return await page.evaluate('[window.fileChanges,window.parseCalls,window.finalSubmitCalls]')

                # Workflow classification has its own regressions; keep the
                # real snapshot, upload, attachment inspection and parse helper.
                with patch.object(service,'workflow_state',AsyncMock(return_value=SimpleNamespace(stage='application_form'))), \
                     patch.object(page,'wait_for_timeout',side_effect=quick_wait):
                    for mode in ('success','async-success'):
                        await reset(mode)
                        result=await service.import_resume_with_site_parser(service.session_id,resume,confirm_site_parse=True)
                        assert result.status=='parsed' and result.trigger_clicked
                        assert result.changed_fields==2,result.model_dump()
                        assert result.uploaded_file==resume.name
                        assert await page.locator('#uploadFile').evaluate('el=>el.files.length')==0
                        assert await counters()==[1,1,0]
                        file=next(f for f in result.snapshot.fields if f.field_type=='file')
                        assert file.current_value=='示例的简历.pdf'
                        assert await page.locator('#profile-name').input_value()=='解析姓名'

                    await reset()
                    result=await service.import_resume_with_site_parser(service.session_id,resume,confirm_site_parse=False)
                    assert result.status=='needs_user_action' and not result.trigger_clicked
                    assert result.changed_fields==0 and await counters()==[1,0,0]
                    assert await page.locator('#LAY_layuipro2').count()==1
                    assert await page.locator('#profile-name').input_value()=='原姓名'

                    for mode,expected in (('old','接收新附件'),('error','上传失败')):
                        await reset(mode,old=True)
                        try:
                            await service.import_resume_with_site_parser(service.session_id,resume,confirm_site_parse=True)
                            raise AssertionError('old/error upload was reported as successful')
                        except ValueError as exc:
                            assert expected in str(exc),str(exc)
                        assert await counters()==[1,0,0]
                        assert await page.locator('#profile-name').input_value()=='原姓名'

                    # A new attachment is valid upload evidence, but unrelated
                    # confirmation text must never become a parse/final click.
                    await reset('wrong-dialog')
                    result=await service.import_resume_with_site_parser(service.session_id,resume,confirm_site_parse=True)
                    assert result.status=='uploaded' and not result.trigger_clicked
                    assert result.changed_fields==0 and await counters()==[1,0,0]
                    assert await page.locator('#LAY_layuipro2 .msg').inner_text()=='确认提交简历？'
                    # A pre-existing dialog blocks another upload before change.
                    try:
                        await service.import_resume_with_site_parser(service.session_id,resume,confirm_site_parse=True)
                        raise AssertionError('pending dialog permitted another upload')
                    except ValueError as exc:
                        assert '待处理弹窗' in str(exc)
                    assert await counters()==[1,0,0]
                assert requests and all(method=='GET' for method,_ in requests),requests
            finally:
                await browser.close()
    print('autohome_native_import_test: OK (real snapshot/import, cleared input, exact parse consent, stale/error rejection, no submits)')


if __name__=='__main__':
    asyncio.run(run())
