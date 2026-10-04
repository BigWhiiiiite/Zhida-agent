"""Anonymous, fully intercepted regression for Autohome field metadata."""
from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright

from app.autohome_fields import refine_autohome_fields


async def run() -> None:
    html = (Path(__file__).parent / "fixtures/autohome_delivery_form.html").read_text()
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        try:
            context = await browser.new_context(service_workers="block")
            await context.route('**/*', lambda route: route.fulfill(body='<html></html>', content_type='text/html'))
            page = await context.new_page()
            await page.goto('https://talent.autohome.com.cn/recruit-delivery.html?pid=47900')
            await page.set_content(html)
            # Mark test-owned selectors once; the production refiner must not
            # mutate DOM or values, including these metadata attributes.
            data = await page.evaluate("""() => [...document.querySelectorAll('input,select')].map((el,i)=>{
              el.setAttribute('data-fixture',String(i));
              return {selector:`[data-fixture="${i}"]`,label:'错误全站导航标题',question_text:'教育经历',required:false};
            })""")
            before = await page.content()
            refined = await refine_autohome_fields(page, data)
            assert await page.content() == before
            assert all(item['label'] == '错误全站导航标题' for item in data)
            dates = [item for item in refined if item['label'] in {'开始时间', '结束时间'}]
            assert len(dates) == 4 and all(item['required'] for item in dates)
            assert dates[0]['container_key'] == dates[1]['container_key']
            assert dates[2]['container_key'] == dates[3]['container_key']
            assert dates[0]['container_key'] != dates[2]['container_key']
            assert all(item['entity_scope'] == 'education:bachelor' for item in dates[:2])
            assert all(item['entity_scope'] == 'education:unspecified' for item in dates[2:])
            assert refined[0]['label'].endswith('（来源类型）')
            assert refined[1]['label'].endswith('（具体来源）')
            assert refined[2]['label'] == '简历附件' and refined[2]['required']
            assert refined[-1]['label'].startswith('我承诺所填简历真实可信') and refined[-1]['required']
            assert refined[-1]['section'] == '诚信承诺'
            assert all('招聘职位 校园招聘' not in item['context'] for item in refined)

            # Selected degree is evidence only for that repeated record.
            await page.locator('select[data-bind*="edut.Education"]').nth(1).select_option(label='硕士')
            selected = await refine_autohome_fields(page, data)
            later = [item for item in selected if item.get('container_key') == dates[2]['container_key']]
            assert later and all(item['entity_scope'] == 'education:master' for item in later)
            # Without the explicit first-record instruction, order means nothing.
            await page.locator('.contentFirstRow .tip').evaluate("el=>el.textContent='请填写教育经历'")
            no_hint = await refine_autohome_fields(page, data)
            assert no_hint[6]['entity_scope'] == 'education:unspecified'
            # A generated Select2 wrapper resolves to its original field too.
            wrapper = [{'selector':'[aria-labelledby="select2-edu2-container"]'}]
            wrapper_result = await refine_autohome_fields(page, wrapper)
            assert wrapper_result[0]['label'] == '学历' and wrapper_result[0]['required']
            assert wrapper_result[0]['entity_scope'] == 'education:master'
            assert await refine_autohome_fields(page,[{'selector':'['}]) == [{'selector':'['}]
            for url in ('https://example.test/recruit-delivery.html',
                        'https://talent.autohome.com.cn/campus-recruit-list.html',
                        'http://talent.autohome.com.cn/recruit-delivery.html'):
                await page.goto(url)
                await page.set_content(html)
                assert await refine_autohome_fields(page, data) is data
        finally:
            await browser.close()
    print('autohome_fields_test: OK (labels, required, Select2, record isolation, explicit scopes, declaration, read-only)')


if __name__ == '__main__':
    asyncio.run(run())
