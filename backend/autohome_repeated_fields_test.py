"""Offline metadata regression for official Autohome work/project templates.

Labels, bindings and sibling boundaries were checked in the anonymous public
https://talent.autohome.com.cn/recruit-delivery.html on 2026-10-03.
The fixture has no remote scripts, account data, uploads or submit handlers.
"""
from __future__ import annotations

import asyncio
from html import escape

from playwright.async_api import async_playwright

from app.autohome_fields import refine_autohome_fields
from app.autohome_sections import discover_autohome_sections
from app.browser_models import PageField
from app.field_semantics import enrich_fields


URL = "https://talent.autohome.com.cn/recruit-delivery.html?pid=47900"
CONFIGS = (
    ("实习/工作经历", "experience", "workTemplate", "addWork", (
        ("Company", "企业名称", "organization"), ("Title", "所任职位", "role"),
        ("StartDate", "开始时间", "start_date"), ("EndDate", "结束时间", "end_date"),
        ("Summary", "工作描述", "description"),
    )),
    ("项目经历", "project", "projectTemplate", "addProject", (
        ("ProjectName", "项目名称", "name"), ("Title", "项目角色", "role"),
        ("StartDate", "开始时间", "start_date"), ("EndDate", "结束时间", "end_date"),
        ("ProjectDescription", "项目描述", "description"),
    )),
)


def fixture() -> str:
    cards = []
    for title, kind, template, handler, fields in CONFIGS:
        records = []
        for number in (1, 2):
            columns = []
            for binding, label, _ in fields:
                identifier = f"{kind}-{number}-{binding}"
                attrs = f'id="{identifier}" data-bind="value: {binding}" datatype="*"'
                value = escape(f"{kind}-{number}-{binding}")
                control = (f'<textarea {attrs}>{value}</textarea>' if binding in {"Summary", "ProjectDescription"}
                           else f'<input {attrs} value="{value}">')
                columns.append(f'<div class="col"><div class="labeltag"><div class="requireTag">*</div>'
                               f'<div class="label">{label}</div></div><div class="control">{control}</div></div>')
            records.append(f'<div class="numtxt">{title}-{number}</div>'
                           f'<div class="row">{"".join(columns[:2])}</div>'
                           f'<div class="row">{"".join(columns[2:4])}</div>'
                           f'<div class="row">{columns[4]}</div>'
                           f'<div class="option"><div class="del">删除{title}</div>'
                           f'<div class="add" data-bind="click: $parent.{handler}">增加{title}</div></div>'
                           '<div class="splitline"></div>')
        cards.append(f'<div class="icard"><div class="title">{title}</div><div class="content">'
                     f'<div data-bind="template:{{name:\'{template}\',foreach:records}}">'
                     f'{"".join(records)}</div></div></div>')
    return ('<!doctype html><html><head><meta charset="utf-8"></head><body>'
            '<nav>教育经历 招聘职位 其他项目</nav><form class="validform">'
            + "".join(cards) + '</form><button>提交简历</button></body></html>')


async def metadata(page):
    return await page.evaluate("""() => [...document.querySelectorAll('input,textarea')].map(el=>({
      selector:'#'+el.id,label:'错误全站导航',question_text:'错误全站导航',required:false,
      semantic_key:'application.custom',entity_scope:'application',
      current_value:el.value,field_type:el.tagName==='TEXTAREA'?'textarea':'text'
    }))""")


async def run() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        try:
            context = await browser.new_context(service_workers="block")
            await context.route("**/*", lambda route: route.fulfill(body="<html></html>", content_type="text/html"))
            page = await context.new_page()
            await page.goto(URL)
            await page.set_content(fixture())
            data = await metadata(page)
            before = await page.content()
            refined = await refine_autohome_fields(page, data)
            assert await page.content() == before
            assert all(item["label"] == "错误全站导航" for item in data)
            assert len(refined) == 20
            fields = enrich_fields([PageField.model_validate(item) for item in refined], URL)
            for section_index, (title, kind, _, _, expected) in enumerate(CONFIGS):
                for number in (1, 2):
                    key = f"autohome:{kind}:{section_index}:{number}"
                    record = [field for field in fields if field.container_key == key]
                    assert len(record) == 5
                    for field, (binding, label, semantic) in zip(record, expected):
                        assert field.selector == f"#{kind}-{number}-{binding}"
                        assert field.label == field.question_text == label
                        assert field.semantic_key == f"{kind}.{semantic}"
                        assert field.entity_scope == f"{kind}:unspecified"
                        assert field.section_path == [title, f"{title}-{number}"]
                        assert field.required and field.current_value == f"{kind}-{number}-{binding}"
            candidates = await discover_autohome_sections(page)
            assert len(candidates) == 2
            assert {key for row in candidates for key in row["record_keys"]} == {field.container_key for field in fields}
            dates = [field for field in fields if field.label in {"开始时间", "结束时间"}]
            assert len(dates) == 8 and len({field.container_key for field in dates}) == 4

            # Missing a delimiter must not merge two records into one group.
            await page.locator('.icard').first.locator('.option').first.evaluate('el=>el.remove()')
            broken = await refine_autohome_fields(page, data)
            assert broken[:10] == data[:10]
            assert all(item.get('container_key', '').startswith('autohome:project:') for item in broken[10:])

            # Repeated anchors and duplicate roots are ambiguous even if labels look valid.
            for mutate in (
                "el=>el.parentElement.append(el.cloneNode(true))",
                "el=>{const root=el.closest('[data-bind*=template]');root.parentElement.append(root.cloneNode(true))}",
                "el=>el.closest('[data-bind*=template]').setAttribute('data-bind',\"template:{name:'workTemplateOther'}\")",
            ):
                await page.set_content(fixture())
                await page.locator('#experience-1-Company').evaluate(mutate)
                result = await refine_autohome_fields(page, data)
                assert result[:10] == data[:10]

            # A duplicate non-anchor never lends another control its question or value.
            await page.set_content(fixture())
            await page.locator('#experience-1-Title').evaluate(
                "el=>{const copy=el.cloneNode(true);copy.id='duplicate-role';el.parentElement.append(copy)}")
            result = await refine_autohome_fields(page, data)
            assert result[1] == data[1]
            assert result[6]['container_key'] == 'autohome:experience:0:2'

            # Visible label and exact data binding must agree; hidden/disabled fields stay untouched.
            await page.set_content(fixture())
            await page.locator('#experience-1-Title').evaluate(
                "el=>el.closest('.col').querySelector('.label').textContent='项目角色'")
            await page.locator('#experience-2-StartDate').evaluate('el=>el.disabled=true')
            await page.locator('#project-1-Title').evaluate("el=>el.closest('.col').style.display='none'")
            result = await refine_autohome_fields(page, data)
            for index in (1, 7, 11):
                assert result[index] == data[index]
            await page.goto('https://example.test/recruit-delivery.html')
            await page.set_content(fixture())
            assert await refine_autohome_fields(page, data) is data
        finally:
            await browser.close()
    print('autohome_repeated_fields_test: OK (20 fields, 4 isolated records, exact labels/bindings, malformed guards, read-only)')


if __name__ == '__main__':
    asyncio.run(run())
