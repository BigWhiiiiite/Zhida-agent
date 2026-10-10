"""Anonymous bank-like deeply nested form; local routed browser fixture only."""
import asyncio
from playwright.async_api import async_playwright
from app.ats_adapters import inspect_application_page


async def run():
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,channel='chrome')
        page=await browser.new_page()
        rows=''.join(f'<div class="row"><div class="label">{caption}</div><div><div><div><div><div><input placeholder="请输入"></div></div></div></div></div></div>'
                     for caption in ['姓名','电子邮箱','手机号码'])
        form='<h2>个人基本信息</h2>'+rows+'<h2>教育经历</h2><button>保存</button>'
        content=[form]
        await page.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html; charset=utf-8',body='<meta charset="utf-8">'+content[0]))
        url='https://eoap.cebbank.com/uiap/wt/CEB/zpzh/resumeEdit?recruitType=1&postId=135601'
        await page.goto(url)
        state=await inspect_application_page(page,'fixture')
        assert state.stage in {'profile_form','application_form'},state
        assert state.form_fields==3 and not state.final_submit_present
        # Neither an application URL nor copied headings without editable
        # controls grants write authority.
        content[0]='<h2>个人基本信息</h2><p>姓名 邮箱 学校</p><h2>教育经历</h2>'
        await page.reload()
        assert (await inspect_application_page(page,'fixture')).stage not in {'profile_form','application_form','review'}
        content[0]=form+'<div role="dialog"><label>密码<input type="password"></label><button>登录</button></div>'
        await page.reload()
        assert (await inspect_application_page(page,'fixture')).stage not in {'profile_form','application_form','review'}
        await browser.close()
    print('bank_form_recognition_test: OK (deep owned captions, no URL-only authority, auth modal gate)')


if __name__=='__main__':asyncio.run(run())
