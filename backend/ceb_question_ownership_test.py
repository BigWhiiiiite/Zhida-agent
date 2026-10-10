"""Anonymous offline regressions for hidden uploads and separate choice owners.

No employer page, applicant values, model, file upload or user action is used.
"""
from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

from app.browser_service import BrowserDemoService, _finalize_choice_metadata
from app.form_field_policy import is_declaration
from app.form_observation import model_page


def upload(title, name, caption_class='el-form-item__label'):
    return ('<div class="el-form-item"><label class="' + caption_class + '">' + title + '</label>'
            '<div class="el-form-item__content"><div class="el-upload">'
            '<button type="button">上传文件</button><input type="file" name="' + name + '" style="display:none">'
            '</div><div class="el-upload__tip">上传文件大小不能超过3M，仅支持PDF或图片</div>'
            '</div></div>')


def source_choices(name='', ant=False):
    return ('<div class="source-question"><div class="question-title">招聘信息来源</div><div class="' +
            ('ant-checkbox-group' if ant else 'choice-list') + '">' +
            ''.join('<label><input type="checkbox" ' + ('name="' + name + '"' if name else '') +
                    '>匿名来源' + str(index) + '</label>' for index in range(14)) + '</div></div>')


def fixture(named=False, ant=False):
    uploads = upload('个人照片', 'photo') + upload('资格材料附件', 'qualification') + upload('补充证明附件', 'additional')
    if ant:
        # The same ownership contract with Ant's outer resume title and deeply
        # wrapped, invisible native input. No employer-specific field strings.
        uploads = uploads.replace('el-form-item__label', 'resume-form-title').replace('class="el-form-item"', 'class="resume-form-item"')
        uploads = uploads.replace('el-form-item__content', 'ant-row ant-form-item').replace('el-upload__tip', 'ant-upload-list-tip')
    return ('<meta charset="utf-8"><main><header>首页 校园招聘 社会招聘 公告 帮助</header><form>' +
            uploads + source_choices('shared-name' if named else '', ant=ant) +
            '<div class="agreement-row"><input type="checkbox" id="agreement" ' +
            ('name="shared-name"' if named else '') +
            '><span>我已阅读并同意<a href="#policy">用户协议</a></span></div>'
            '<button type="submit">提交申请</button></form></main>'
            '<script>window.actions=[];for(const type of ["click","input","change","submit"])'
            'document.addEventListener(type,()=>window.actions.push(type));</script>')


async def run():
    for owned, source in ((False, 'container-owned'), (True, 'nearby')):
        unverified = {'selector':'#anonymous', 'field_type':'checkbox', 'control_group_key':'single',
                      'question_text':'我已阅读并同意用户协议', 'group_label':'我已阅读并同意用户协议',
                      'options':['我已阅读并同意用户协议'], 'option_label':'我已阅读并同意用户协议',
                      'label_source':source, 'question_candidates':[{'text':'我已阅读并同意用户协议',
                                                                   'source':source, 'owned':owned}]}
        _finalize_choice_metadata([unverified])
        assert unverified['question_text'].startswith('未识别'), unverified
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            await context.route('**/*', lambda route: route.abort())
            await context.route_web_socket('**/*', lambda socket: socket.close())
            page = await context.new_page()
            service = BrowserDemoService()
            service.page, service.context, service.session_id = page, context, 'anonymous-ownership-fixture'
            for named, ant, broad in ((False, False, False), (True, False, False), (False, True, False),
                                      (True, True, False), (False, True, True), (True, True, True)):
                html=fixture(named,ant)
                if broad:
                    html=html.replace('<form>', '<form><div role="group">').replace('</form>', '</div></form>')
                await page.set_content(html)
                snapshot = await service.snapshot(probe_options=False)
                assert len(snapshot.fields) == 18, len(snapshot.fields)
                for name, title in [('photo', '个人照片'), ('qualification', '资格材料附件'), ('additional', '补充证明附件')]:
                    field = next(field for field in snapshot.fields if field.name == name)
                    assert field.question_text == title, (name, field.question_text)
                    assert field.label_source == 'container-owned', field.model_dump()
                    assert field.observation.question_status == 'verified', field.observation
                    assert all('3M' not in evidence.text for evidence in field.question_candidates if evidence.owned)
                agreement_marker = await page.locator('#agreement').get_attribute('data-zhida-field')
                agreement = next(field for field in snapshot.fields if agreement_marker in field.selector)
                sources = [field for field in snapshot.fields if field.field_type == 'checkbox' and field is not agreement]
                assert len(sources) == 14
                assert len({field.control_group_key for field in sources}) == 1
                assert agreement.control_group_key not in {field.control_group_key for field in sources}
                assert all(field.question_text == '招聘信息来源' for field in sources)
                assert all(len(field.options) == 14 and not any('协议' in option for option in field.options) for field in sources)
                assert is_declaration(agreement), agreement.model_dump()
                assert agreement.observation.question_status == 'verified', agreement.model_dump()
                assert len(agreement.options) == 1 and '协议' in agreement.options[0]
                assert '首页' not in agreement.question_text and '匿名来源' not in agreement.question_text
                assert len(model_page(snapshot)['questions']) == 5
                assert await page.evaluate('window.actions') == []
                assert not await page.locator('#agreement').is_checked()

            # Equal native names are not evidence that two locally labelled
            # questions belong together; all names below are deliberately equal.
            await page.set_content('<main><form>' + ''.join(
                '<div class="form-field"><div class="question-title">' + title + '</div>'
                '<div><label><input type="checkbox" name="same">甲</label>'
                '<label><input type="checkbox" name="same">乙</label></div></div>'
                for title in ['技术领域偏好', '工作城市偏好']) + '</form></main>')
            separate = await service.snapshot(probe_options=False)
            assert len({field.control_group_key for field in separate.fields}) == 2
            assert {field.question_text for field in separate.fields} == {'技术领域偏好', '工作城市偏好'}

            # A caption wrapper containing only upload restrictions does not
            # become an owned title; helpers nested inside labels stay helpers.
            await page.set_content('<div class="form-field"><div class="caption"><small>'
                                   '最多上传一个PDF，不超过3M</small></div><div><button>上传文件</button>'
                                   '<input type="file" style="display:none"></div></div>')
            helper = await service.snapshot(probe_options=False)
            assert len(helper.fields)==1 and helper.fields[0].observation.question_status!='verified'
            assert '3M' not in helper.fields[0].question_text

            # Title comparison and required evidence use the same helper-free
            # caption. A title's star survives; a helper's star stays a helper.
            for caption, required in [('个人照片＊<small>文件大小不超过3M</small>', True),
                                      ('个人照片<small>注意文件格式＊</small>', False),
                                      ('个人照片<small><i class="required">＊</i>注意文件格式</small>', False)]:
                await page.set_content('<div class="el-form-item"><label class="el-form-item__label">' +
                                       caption + '</label><div><button>上传文件</button>'
                                       '<input type="file" style="display:none"></div></div>')
                with_help = await service.snapshot(probe_options=False)
                photo = with_help.fields[0]
                assert photo.question_text=='个人照片' and photo.observation.question_status=='verified', photo.model_dump()
                assert photo.required is required, photo.model_dump()
                assert ('当前题目标签的必填标记' in photo.required_evidence) is required

            # A generic .field wrapper can be one OPTION rather than an entire
            # question. Keep the shared caption and actual two-option boundary.
            await page.set_content('<div><div class="question-title">招聘信息来源</div>'
                                   '<div class="field"><label><input type="checkbox">校园网站</label></div>'
                                   '<div class="field"><label><input type="checkbox">招聘平台</label></div></div>')
            options = await service.snapshot(probe_options=False)
            assert len(options.fields)==2 and len({field.control_group_key for field in options.fields})==1
            assert all(field.question_text=='招聘信息来源' and field.observation.question_status=='verified'
                       and field.options==['校园网站','招聘平台'] for field in options.fields)

            # No local caption: leave ownership unresolved instead of borrowing
            # navigation, a sibling upload title or a neighbouring choice group.
            await page.set_content('<main><header>首页 招聘 联系我们</header><form>' + upload('证书材料', 'owned') +
                                   '<div><input type="file" name="orphan" style="display:none">'
                                   '<small>文件大小不能超过3M</small></div>' + source_choices() +
                                   '<div><input type="checkbox" id="orphan-choice"></div></form></main>')
            unresolved = await service.snapshot(probe_options=False)
            orphan = next(field for field in unresolved.fields if field.name == 'orphan')
            assert orphan.observation.question_status != 'verified', orphan.model_dump()
            assert '3M' not in orphan.question_text and '首页' not in orphan.question_text
            marker = await page.locator('#orphan-choice').get_attribute('data-zhida-field')
            single = next(field for field in unresolved.fields if marker in field.selector)
            assert len(single.options) <= 1 and '首页' not in single.question_text
        finally:
            await browser.close()
    print('ceb_question_ownership_test: OK (anonymous local upload captions, distinct declaration/source groups, no writes)')


if __name__ == '__main__':
    asyncio.run(run())
