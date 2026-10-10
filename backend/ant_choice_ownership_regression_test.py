"""Independent anonymous real-Ant DOM ownership regression; no IO/model/network.

The main fixture has 93 native controls but 68 logical questions: 54 single
inputs, 12 two-option radio questions, a 14-option source question, and one
independent declaration. Option wrappers deliberately contain Ant class names
that look like form/group owners; those wrappers do not own whole questions.
"""
from __future__ import annotations

import asyncio
from html import escape
from unittest.mock import patch

from playwright.async_api import async_playwright

from app.browser_service import BrowserDemoService
from app.form_field_policy import is_declaration
from app.form_observation import extraction_block_reason, logical_questions


SOURCE_OPTIONS = [f"匿名来源{index:02d}" for index in range(1, 15)]
DECLARATION = "本人声明所填资料真实并承担相应法律责任"
STYLES = """<style>
body{font:14px sans-serif;width:950px}.resume-tpl-wrap{padding:12px;border:1px solid #aaa}
.resume-tpl-title{font-size:18px;font-weight:bold}.resume-form-item{padding:6px}
.resume-form-title{display:block;margin-bottom:4px}.ant-radio-wrapper,.ant-checkbox-wrapper{display:inline-block;position:relative;margin-right:12px}
.ant-radio,.ant-checkbox{display:inline-block;width:16px;height:16px;vertical-align:middle}
.ant-radio-input,.ant-checkbox-input{position:absolute;opacity:0;width:16px;height:16px}
.ant-radio-inner,.ant-checkbox-inner{display:block;width:14px;height:14px;border:1px solid #777}
.ant-form-item-explain,small{font-size:11px;color:#777}
</style>"""


def native_name(mode, question):
    return "" if mode == "absent" else ' name="' + ("all-controls" if mode == "shared" else escape(question)) + '"'


def option(kind, key, caption, value, mode, question):
    group_item = " ant-checkbox-group-item" if kind == "checkbox" else ""
    return ('<label class="ant-' + kind + '-wrapper ant-' + kind + '-wrapper-in-form-item' + group_item + '">'
            '<span class="ant-' + kind + '"><input id="' + key + '" class="ant-' + kind + '-input" type="' + kind + '"'
            + native_name(mode, question) + ' value="' + escape(value) + '">'
            '<span class="ant-' + kind + '-inner"></span></span><span>' + escape(caption) + '</span></label>')


def item(title, control, *, required=False, helper_star=False):
    caption = ('<div class="resume-form-title">' + ('<span class="required">＊</span>' if required else '')
               + '<span>' + escape(title) + '</span>'
               + ('<small>匿名填写说明＊</small>' if helper_star else '') + '</div>') if title else ''
    return ('<div class="resume-form-item">' + caption + '<div class="ant-row"><div class="ant-col ant-col-24">'
            '<div class="ant-form-item"><div class="ant-row ant-form-item-row"><div class="ant-col ant-form-item-control">'
            '<span class="ant-form-item-children">' + control + '</span></div></div></div></div></div></div>')


def choice(kind, key, title, captions, mode, *, required=False, helper_star=False):
    controls = ''.join(option(kind, f"{key}-{index}", caption, f"value-{index}", mode, key)
                       for index, caption in enumerate(captions))
    return item(title, '<div class="ant-' + kind + '-group ant-' + kind + '-group-outline">' + controls + '</div>',
                required=required, helper_star=helper_star)


def section(title, body):
    return '<div class="resume-tpl-wrap"><div class="resume-tpl-title">' + escape(title) + '</div><div class="resume-tpl-content">' + body + '</div></div>'


def full_fixture(mode):
    expected = {}
    parts = []
    for section_index, section_title in enumerate(("基本信息", "教育经历", "其他信息")):
        content = []
        for offset in range(18):
            index = section_index * 18 + offset
            key, title = f"text-{index}", f"匿名事项{index + 1:02d}"
            required = index % 3 == 0
            expected[key] = {"question": title, "section": section_title, "required": required, "kind": "text"}
            content.append(item(title, '<input id="' + key + '" class="ant-input" type="text"'
                                + native_name(mode, key) + '>', required=required))
        for offset in range(4):
            index = section_index * 4 + offset
            key = f"radio-{index}"
            # Equal question text in different independent DOM groups must not
            # merge merely because a native name or answer captions match.
            title = "同名匿名确认题" if index in {0, 1} else f"匿名确认题{index + 1:02d}"
            required = index % 2 == 0
            content.append(choice("radio", key, title, ["是", "否"], mode, required=required))
            for option_index in range(2):
                expected[f"{key}-{option_index}"] = {"question": title, "section": section_title,
                    "required": required, "kind": "radio", "group": key, "options": ["是", "否"],
                    "option_label": ["是", "否"][option_index], "option_value": f"value-{option_index}"}
        if section_title == "其他信息":
            content.append(choice("checkbox", "sources", "招聘信息来源", SOURCE_OPTIONS, mode, required=True))
            for index in range(14):
                expected[f"sources-{index}"] = {"question": "招聘信息来源", "section": section_title,
                    "required": True, "kind": "checkbox", "group": "sources", "options": SOURCE_OPTIONS,
                    "option_label": SOURCE_OPTIONS[index], "option_value": f"value-{index}"}
        parts.append(section(section_title, ''.join(content)))
    statement = option("checkbox", "declaration", DECLARATION, "agree", mode, "sources")
    parts.append(section("诚信声明", item(DECLARATION, statement, required=True)))
    expected["declaration"] = {"question": DECLARATION, "section": "诚信声明", "required": True,
                               "kind": "checkbox", "group": "declaration", "options": [DECLARATION],
                               "option_label": DECLARATION, "option_value": "agree"}
    return document(''.join(parts)), expected


def document(body):
    return ('<meta charset="utf-8">' + STYLES + '<main><header>首页 招聘 帮助 匿名导航</header><form>' + body
            + '<button type="submit">提交申请</button></form></main>'
            '<script>window.actions=[];for(const type of ["click","input","change","submit"])'
            'document.addEventListener(type,event=>window.actions.push(type+":"+(event.target.id||event.target.tagName)));</script>')


async def fields_by_id(page, snapshot):
    markers = await page.evaluate('() => [...document.querySelectorAll("input")].map(node=>({id:node.id,marker:node.getAttribute("data-zhida-field")}))')
    return {entry["id"]: next((field for field in snapshot.fields if entry["marker"] and entry["marker"] in field.selector), None)
            for entry in markers}


async def run():
    failures, blocked_requests = [], []
    def check(condition, message):
        if not condition:
            failures.append(message)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = await browser.new_context(service_workers="block", viewport={"width": 1280, "height": 900})
            async def deny_request(route):
                blocked_requests.append(route.request.url)
                await route.abort()
            async def deny_socket(socket):
                blocked_requests.append("WebSocket")
                await socket.close()
            await context.route("**/*", deny_request)
            await context.route_web_socket("**/*", deny_socket)
            page = await context.new_page()
            service = BrowserDemoService()
            service.page, service.context, service.session_id = page, context, "anonymous-ant-ownership"
            with patch("app.storage._connection", side_effect=AssertionError("no personal DB")):
                for mode in ("absent", "shared", "per-question"):
                    html, expected = full_fixture(mode)
                    await page.set_content(html)
                    captured = await service.snapshot(probe_options=False)
                    by_id = await fields_by_id(page, captured)
                    questions = logical_questions(captured.fields)
                    check(len(captured.fields) == 93, f"{mode}: captured {len(captured.fields)} controls, expected 93")
                    check(len(questions) == 68, f"{mode}: logical questions {len(questions)}, expected 68")
                    bad_titles, bad_required, bad_sections, bad_evidence, bad_choices = [], [], [], [], []
                    for key, wanted in expected.items():
                        field = by_id.get(key)
                        if field is None:
                            failures.append(f"{mode}: native control {key} missing from capture")
                            continue
                        if field.question_text != wanted["question"]: bad_titles.append((key, field.question_text))
                        if field.required is not wanted["required"]: bad_required.append((key, field.required))
                        if field.section != wanted["section"] or wanted["section"] not in field.section_path:
                            bad_sections.append((key, field.section, field.section_path))
                        if field.observation.question_status != "verified": bad_evidence.append((key, field.observation.question_status))
                        if wanted["kind"] in {"radio", "checkbox"} and (
                                field.option_label != wanted["option_label"]
                                or field.option_value != wanted["option_value"]
                                or field.observation.options_status != "group_complete"):
                            bad_choices.append((key, field.option_label, field.option_value, field.observation.options_status))
                    check(not bad_titles, f"{mode}: wrong owned titles {bad_titles[:8]} ({len(bad_titles)} total)")
                    check(not bad_required, f"{mode}: wrong star-required {bad_required[:8]} ({len(bad_required)} total)")
                    check(not bad_sections, f"{mode}: wrong owning sections {bad_sections[:8]} ({len(bad_sections)} total)")
                    check(not bad_evidence, f"{mode}: unverified owned questions {bad_evidence[:8]} ({len(bad_evidence)} total)")
                    check(not bad_choices, f"{mode}: wrong own option identity/completeness {bad_choices[:8]} ({len(bad_choices)} total)")
                    radio_keys = []
                    for index in range(12):
                        members = [by_id.get(f"radio-{index}-{option_index}") for option_index in range(2)]
                        if any(member is None for member in members): continue
                        keys = {member.control_group_key for member in members}
                        check(len(keys) == 1 and bool(next(iter(keys))), f"{mode}: radio-{index} split into {keys}")
                        check(all(member.options == ["是", "否"] for member in members), f"{mode}: radio-{index} real options incomplete")
                        radio_keys.extend(keys)
                    check(len(set(radio_keys)) == 12, f"{mode}: expected 12 independent radio owners, got {len(set(radio_keys))}")
                    sources = [by_id.get(f"sources-{index}") for index in range(14)]
                    if all(field is not None for field in sources):
                        source_keys = {field.control_group_key for field in sources}
                        check(len(source_keys) == 1 and bool(next(iter(source_keys))), f"{mode}: 14 source choices split into {len(source_keys)} groups")
                        check(all(field.options == SOURCE_OPTIONS for field in sources), f"{mode}: source options contain missing/foreign captions")
                        declaration = by_id.get("declaration")
                        if declaration:
                            check(declaration.control_group_key not in source_keys, f"{mode}: independent declaration merged with source choices")
                            check(is_declaration(declaration), f"{mode}: independent declaration was not recognized")
                            check(declaration.options == [DECLARATION], f"{mode}: declaration captured foreign options: {declaration.options}")
                    check(await page.evaluate("window.actions") == [], f"{mode}: observation dispatched form actions")
                    check(await page.locator('input:checked').count() == 0, f"{mode}: observation selected an answer")
                    print(f"Ant ownership evidence [{mode}]: controls={len(captured.fields)} logical={len(questions)}"
                          f" wrong_titles={len(bad_titles)} wrong_required={len(bad_required)} wrong_sections={len(bad_sections)}")

                # A missing own title is not permission to borrow an adjacent
                # owned title, a section heading, navigation, or helper prose.
                for kind in ("radio", "checkbox"):
                    orphan = choice(kind, "orphan", "", ["是", "否"], "shared")
                    neighbor = item("邻题的到岗城市", '<input id="neighbor" type="text">')
                    await page.set_content(document(section("其他信息", neighbor + orphan)))
                    captured = await service.snapshot(probe_options=False)
                    by_id = await fields_by_id(page, captured)
                    for index in range(2):
                        field = by_id.get(f"orphan-{index}")
                        check(field is not None, f"orphan {kind}: missing native option {index}")
                        if field:
                            check(field.observation.question_status != "verified", f"orphan {kind}: false owned title '{field.question_text}'")
                            check("邻题的到岗城市" not in field.question_text and field.question_text != "其他信息",
                                  f"orphan {kind}: borrowed unrelated title '{field.question_text}'")

                # Stars inside helper text cannot create a required marker.
                await page.set_content(document(section("其他信息", choice("radio", "helper-star", "匿名提醒偏好", ["是", "否"], "absent", helper_star=True))))
                captured = await service.snapshot(probe_options=False)
                for field in captured.fields:
                    check(field.question_text == "匿名提醒偏好", f"helper-star: helper polluted title '{field.question_text}'")
                    check(not field.required, "helper-star: explanation star incorrectly marked the question required")

                # A declared Ant radio group cannot override native radio
                # semantics: different names, or one named and one unnamed
                # input, do not form one coherent native single-choice group.
                for naming, names in (("different", ["anonymous-a", "anonymous-b"]),
                                      ("partly-missing", ["anonymous-a", ""])):
                    await page.set_content(document(section("其他信息", choice(
                        "radio", "native-names", "匿名原生名字一致性题", ["是", "否"], "absent"))))
                    await page.evaluate('(names) => document.querySelectorAll("input[type=radio]").forEach((input,index)=>input.name=names[index])', names)
                    captured = await service.snapshot(probe_options=False)
                    check(len(captured.fields) == 2, f"native names {naming}: missing native radio controls")
                    check(len(logical_questions(captured.fields)) == 2,
                          f"native names {naming}: contradictory native controls merged into one logical question")
                    for field in captured.fields:
                        check(field.options != ["是", "否"],
                              f"native names {naming}: contradictory native controls retained a full shared option group")
                        safe_complete = (field.observation.question_status == "verified"
                                         and field.observation.options_status == "group_complete"
                                         and not extraction_block_reason(field))
                        check(not safe_complete,
                              f"native names {naming}: inconsistent native radios blessed as a complete owned question")
                    check(await page.evaluate("window.actions") == [], f"native names {naming}: observation dispatched actions")

                # Real templates also use a caption class or a classless
                # header, sometimes with an actual +add button in the header.
                # They are section evidence, never the field's own question.
                for shape, header in (
                        ("caption", '<div class="resume-tpl-caption">教育经历</div>'),
                        ("classless", '<div>教育经历</div>'),
                        ("caption-add", '<div class="resume-tpl-caption"><span>教育经历</span><button type="button">＋添加</button></div>')):
                    body = ('<div class="resume-tpl-wrap">' + header + '<div class="resume-tpl-content">'
                            + item("匿名本栏目字段", '<input id="section-shape" type="text">') + '</div></div>')
                    await page.set_content(document(body))
                    captured = await service.snapshot(probe_options=False)
                    field = (await fields_by_id(page, captured)).get("section-shape")
                    check(field is not None, f"section {shape}: missing owned text field")
                    if field:
                        check(field.question_text == "匿名本栏目字段", f"section {shape}: header polluted question '{field.question_text}'")
                        check(field.section == "教育经历" and "教育经历" in field.section_path,
                              f"section {shape}: lost own section '{field.section}' / {field.section_path}")
                    check(await page.evaluate("window.actions") == [], f"section {shape}: observation clicked +add or dispatched actions")

                # A CSS-hidden star is not required evidence. Turning the same
                # attached DOM mark visible on the next snapshot must refresh
                # short-lived caption evidence, not reuse the previous copy.
                hidden_star = choice("radio", "css-star", "匿名星号显示题", ["是", "否"], "absent")
                hidden_star = hidden_star.replace('<div class="resume-form-title">',
                    '<div class="resume-form-title"><span class="anonymous-hidden-mark">＊</span>', 1)
                await page.set_content(document('<style>.anonymous-hidden-mark{display:none}</style>'
                                               + section("其他信息", hidden_star)))
                for visible in (False, True):
                    if visible:
                        await page.locator('.anonymous-hidden-mark').evaluate("node=>node.style.display='inline'")
                    captured = await service.snapshot(probe_options=False)
                    for field in captured.fields:
                        check(field.question_text == "匿名星号显示题", f"CSS star visible={visible}: polluted question '{field.question_text}'")
                        check(field.required is visible,
                              f"CSS star visible={visible}: incorrect required={field.required}; hidden/cached caption evidence")
                    check(await page.evaluate("window.actions") == [], f"CSS star visible={visible}: observation dispatched actions")

                # A declared radio group containing a checkbox (or the reverse)
                # is inconsistent type evidence; do not bless it as a complete
                # automatic single-choice/multi-choice fact question.
                for declared_kind in ("radio", "checkbox"):
                    mixed = ('<div class="ant-' + declared_kind + '-group">'
                             + option("radio", "mixed-radio", "选项甲", "a", "shared", "mixed")
                             + option("checkbox", "mixed-checkbox", "选项乙", "b", "shared", "mixed") + '</div>')
                    await page.set_content(document(section("其他信息", item("匿名混合类型题", mixed))))
                    captured = await service.snapshot(probe_options=False)
                    check(len(captured.fields) == 2, f"mixed {declared_kind}: wrong native control count {len(captured.fields)}")
                    check(all(extraction_block_reason(field) or field.observation.question_status != "verified"
                              for field in captured.fields), f"mixed {declared_kind}: inconsistent types blessed as complete questions "
                          + str([(field.field_type, field.question_text, field.observation.model_dump()) for field in captured.fields]))
                    check(await page.evaluate("window.actions") == [], f"mixed {declared_kind}: observation dispatched actions")
            check(not blocked_requests, f"unexpected attempted network/socket requests: {blocked_requests}")
        finally:
            await browser.close()
    if failures:
        print("Ant ownership regression failures:")
        for failure in failures: print(" - " + failure)
        raise AssertionError(f"{len(failures)} anonymous Ant ownership contract violations")
    print("ant_choice_ownership_regression_test: OK (93 controls/68 questions, real Ant groups/native names, section ownership/header shapes, visible/hidden stars, missing/mixed evidence, no actions)")


if __name__ == "__main__":
    asyncio.run(run())
