"""Offline regressions for deep question ownership and recognition diagnostics."""
from __future__ import annotations

import asyncio
import json

from playwright.async_api import async_playwright

from app.browser_service import BrowserDemoService
from app.recognition_diagnostics import inspect_recognition_structure


def wrapped(content, depth=10):
    for _ in range(depth):
        content = '<div class="ComponentShell">' + content + '</div>'
    return content


HTML = '''<header><div class="nav-label">首页 退出登录</div>
<input placeholder="搜索职位关键词"></header>
<main><h1>申请表</h1>''' + ''.join(
    '<div class="DeepQuestion"><div class="QuestionCaption">' + label + '</div>' +
    wrapped(control) + '</div>' for label, control in [
        ('姓名', '<input required value="anonymous-value">'),
        ('称呼', '<input>'),
        ('学校名称', '<input>'),
        ('专业名称', '<input>'),
    ]) + '''<section><h2>教育经历</h2><input aria-label="教育记录备注"></section>
<textarea readonly style="visibility:hidden">hidden-value</textarea>
<script>window.secret='never-in-a-caption';</script><textarea readonly></textarea>
<input type="password" value="not-for-diagnostics">
<button type="submit" onclick="document.body.dataset.submitted='yes'">提交申请</button></main>'''


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            requests = []

            async def serve(route):
                requests.append(route.request.url)
                await route.fulfill(body=HTML, content_type='text/html; charset=utf-8')

            await context.route('**/*', serve)
            await context.route_web_socket('**/*', lambda socket: socket.close())
            page = await context.new_page()
            await page.goto('https://fixture.zhiye.com/form?jobAdId=offline-only')
            service = BrowserDemoService()
            service.page, service.context, service.session_id = page, context, 'fixture'
            snapshot = await service.snapshot()
            labels = [f.label for f in snapshot.fields]
            assert len(labels) == 5, labels
            for label in ('姓名', '称呼', '学校名称', '专业名称'):
                field = next(f for f in snapshot.fields if f.label == label)
                assert field.label_source == 'container-owned', field.model_dump()
                assert field.recognition_confidence >= .85
            assert next(f for f in snapshot.fields if f.label == '姓名').required
            assert not any('搜索' in label or 'secret' in label for label in labels)
            assert next(f for f in snapshot.fields if f.label == '称呼').semantic_key != 'candidate.name'
            diagnostics = await inspect_recognition_structure(page)
            serialized = json.dumps(diagnostics, ensure_ascii=False)
            assert 'anonymous-value' not in serialized and 'hidden-value' not in serialized
            assert 'not-for-diagnostics' not in serialized and 'never-in-a-caption' not in serialized
            assert 'DeepQuestion' in serialized and '学校名称' in serialized
            assert all('value' not in row for row in diagnostics['controls'])
            assert await page.locator('body').get_attribute('data-submitted') is None
            # A shared caption cannot establish ownership of two text controls.
            await page.set_content('<div><div class="QuestionCaption">姓名</div>' +
                                   wrapped('<input><input>') + '</div>')
            ambiguous = await service.snapshot()
            assert all(f.label_source != 'container-owned' for f in ambiguous.fields)
            # CSS-hidden ancestors and invisible tooling must not become fields.
            await page.set_content('<div style="visibility:hidden"><input aria-label="隐形姓名"></div>'
                                   '<label>真实题目<input></label>')
            visible = await service.snapshot()
            assert len(visible.fields) == 1 and visible.fields[0].label == '真实题目'
            assert all(url.startswith('https://fixture.zhiye.com/') for url in requests)
        finally:
            await browser.close()
    print('deep_question_snapshot_test: OK (owned labels, navigation/tool filtering, no values in diagnostics, no writes)')


if __name__ == '__main__':
    asyncio.run(run())
