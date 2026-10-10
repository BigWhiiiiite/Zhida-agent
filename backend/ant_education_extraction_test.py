"""Anonymous read-only Ant education records; no real site, model or fills."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import patch

from playwright.async_api import async_playwright

from app.ant_resume_records import inspect_ant_resume_records, inspect_ant_resume_rejections
from app.browser_service import BrowserDemoService
from app.field_semantics import normalize_text, option_fingerprint
from app.form_agent import _education_resolutions, create_local_form_plan
from app.form_observation import _record_observed, model_page
from app.models import ApplicationAnswerMemory, CandidateProfile, Education


CAPTIONS = ('学校名称', '学历', '学院名称', '专业名称', '入学时间', '毕业时间', '学校所在地',
            '是否统招', '学制', '导师', '专业排名', 'GPA', '研究方向', '学号', '实验室')


def row(caption, value='', group='', checkbox=False):
    control = '<input value="' + value + '">'
    if caption in {'入学时间', '毕业时间'}:
        control = '<div class="ant-picker"><div class="ant-picker-input"><input readonly></div></div>'
    elif caption in {'是否统招', '是否最高学历'}:
        if checkbox:
            control = '<label class="ant-checkbox-wrapper ant-checkbox-wrapper-in-form-item"><span class="ant-checkbox"><input type="checkbox"></span><span>是</span></label>'
        else:
            control = '<div class="ant-radio-group">' + ''.join(
                '<label class="ant-radio-wrapper ant-radio-wrapper-in-form-item"><span class="ant-radio">'
                '<input type="radio" name="' + group + '"></span><span>' + option + '</span></label>'
                for option in ('是', '否')) + '</div>'
    elif caption == '上传成绩单':
        control = '<div class="ant-upload"><button type="button">上传文件</button><input type="file" style="display:none"></div><small>附件不超过3M</small>'
    elif caption == '学历':
        control = '<select><option>请选择</option><option>本科</option><option>硕士</option></select>'
    return ('<div class="resume-form-item"><div class="resume-form-title">' + caption + '</div>'
            '<div class="field-input-style"><div class="ant-row ant-form-item"><div class="ant-col ant-form-item-control">'
            '<div class="ant-form-item-control-input"><div class="ant-form-item-control-input-content">'
            '<div class="field-item-wrap">' + control + '</div></div></div></div></div></div></div>')


def record(school, group, extra=''):
    return '<div class="resume-form-wrap">' + ''.join(
        row(caption, school if caption == '学校名称' else '', group) for caption in CAPTIONS) + extra + '</div>'


def section(body):
    return '<div class="resume-tpl-wrap"><div class="section-title">教育经历</div><div>' + body + '</div></div>'


async def _run():
    profile = CandidateProfile(education=[
        Education(school='匿名甲大学', college='仅甲学院', major='仅甲专业', degree='本科'),
        Education(school='匿名乙大学', college='仅乙学院', major='仅乙专业', degree='硕士')])
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            await context.route('**/*', lambda route: route.abort())
            await context.route_web_socket('**/*', lambda socket: socket.close())
            page = await context.new_page()
            # Deliberately reverse source order. Two 15-question records must
            # not become 30 question-containers or gain order-based bindings.
            html = '<meta charset="utf-8">' + section(record('匿名乙大学', 'row-b') + record('匿名甲大学', 'row-a'))
            await page.set_content(html + '''<script>
window.events=0;for(const type of ['click','change','input','submit'])
document.addEventListener(type,()=>window.events++);
</script>''')
            service = BrowserDemoService()
            service.page, service.context, service.session_id = page, context, 'education-fixture'
            snapshot = await service.snapshot(probe_options=False)
            anchors = [f for f in snapshot.fields if f.semantic_key == 'education.school']
            assert len(anchors) == 2 and len({f.container_key for f in anchors}) == 2
            for anchor in anchors:
                members = [f for f in snapshot.fields if f.container_key == anchor.container_key]
                assert len(members) == 16  # the 15th logical question has two radio members
                assert {f.section for f in members} == {'教育经历'}
                assert all(f.record_evidence == 'ant-resume-owned' for f in members)
                assert {'education.school', 'education.college', 'education.major',
                        'education.start_date', 'education.end_date'}.issubset({f.semantic_key for f in members})
            inventory = await inspect_ant_resume_records(page)
            assert len(inventory) == 1 and inventory[0]['record_count'] == 2
            assert inventory[0]['semantic_section'] == 'education' and not inventory[0]['selector']
            diagnostic = await inspect_ant_resume_rejections(page)
            assert all(item['verified'] and item['row_count'] == 15 for item in diagnostic['records'])
            assert '匿名甲大学' not in str(diagnostic) and '匿名乙大学' not in str(diagnostic)
            plan = create_local_form_plan(snapshot, profile)
            for anchor in anchors:
                expected = next(e for e in profile.education if e.school == anchor.current_value)
                members = [f for f in snapshot.fields if f.container_key == anchor.container_key]
                for attribute in ('college', 'major'):
                    field = next(f for f in members if f.semantic_key == 'education.' + attribute)
                    action = next(a for a in plan.actions if a.selector == field.selector)
                    assert action.value == getattr(expected, attribute), (attribute, anchor.current_value, action.value)
            model_questions = model_page(snapshot)['questions']
            assert len(model_questions) == 30
            assert all(q['record_context']['container_key'] in {f.container_key for f in anchors}
                       for q in model_questions)
            assert await page.evaluate('window.events') == 0

            # The observed initial education shell has no school anchor. It is
            # a local surface, not a personal education record or allocation.
            for checkbox in (False, True):
                shell = '<div class="resume-form-wrap">' + row('是否最高学历', group='shell', checkbox=checkbox) + row('学历') + row('上传成绩单') + '</div>'
                await page.set_content('<meta charset="utf-8">' + section(shell) + '<script>window.events=0;for(const type of ["click","input","change","submit"])document.addEventListener(type,()=>window.events++)</script>')
                shell_snapshot = await service.snapshot(probe_options=False)
                diagnostic = await inspect_ant_resume_rejections(page)
                assert not await inspect_ant_resume_records(page)
                assert diagnostic['records'][0]['reason'] == 'identity_anchor_unverified'
                assert diagnostic['records'][0]['surface_observed'] and not diagnostic['records'][0]['verified']
                assert diagnostic['records'][0]['section'] == '教育经历'
                assert diagnostic['records'][0]['row_count'] == 3 and diagnostic['records'][0]['unknown_question_count'] == 0
                assert not any(f.record_evidence == 'ant-resume-owned' for f in shell_snapshot.fields)
                assert len(shell_snapshot.fields) == (3 if checkbox else 4)
                assert len({f.container_key for f in shell_snapshot.fields}) == 1
                assert all(f.container_key.startswith('ant-shell-observed:') and
                           not f.container_key.startswith('ant-resume-') for f in shell_snapshot.fields)
                assert all(f.record_evidence == 'ant-resume-shell-observed' and
                           f.section_path == ['教育经历'] and f.entity_scope == 'education:unspecified'
                           for f in shell_snapshot.fields)
                assert all(not _record_observed(f) for f in shell_snapshot.fields)
                degree = next(f for f in shell_snapshot.fields if f.question_text == '学历')
                assert degree.semantic_key == 'education.degree'
                assert degree.observation.record_status == 'unresolved'
                assert all(f.semantic_key == 'application.custom' for f in shell_snapshot.fields
                           if f.question_text in {'是否最高学历', '上传成绩单'})
                assert all(f.observation.question_status == 'verified' for f in shell_snapshot.fields)
                assert len(model_page(shell_snapshot)['questions']) == 3
                # Structural observation cannot become a personal binding
                # through a sole CV record, an existing degree selection or a
                # remembered answer for a similarly named auxiliary question.
                highest = next(f for f in shell_snapshot.fields if f.question_text == '是否最高学历')
                memory = ApplicationAnswerMemory(id='anonymous-shell-answer', question=highest.question_text,
                    normalized_question=normalize_text(highest.question_text), semantic_key=highest.semantic_key,
                    entity_scope=highest.entity_scope, field_signature=highest.field_signature,
                    field_type=highest.field_type, option_fingerprint=option_fingerprint(highest.options),
                    value='是', resume_id='anonymous-resume', company_scope='anonymous-company',
                    updated_at=datetime.now(timezone.utc))
                single = CandidateProfile(education=[profile.education[0]],
                    application_answer_memory=[memory], application_answers={'是否最高学历': '是'})
                for selected in ('', '本科'):
                    observed_shell = shell_snapshot.model_copy(update={'fields':[
                        f.model_copy(update={'current_value': selected}) if f.selector == degree.selector else f
                        for f in shell_snapshot.fields]})
                    assert all(result.record is None for result in _education_resolutions(observed_shell, single).values())
                    shell_plan = create_local_form_plan(observed_shell, single)
                    assert not any(action.action in {'fill', 'select', 'check', 'upload'}
                                   for action in shell_plan.actions)
                assert await page.evaluate('window.events') == 0

            # A neighboring anchored record does not supply the shell's
            # missing identity, and the shell must not enter its slot list.
            shell = '<div class="resume-form-wrap">' + row('是否最高学历', group='neighbor-shell') + row('学历') + row('上传成绩单') + '</div>'
            await page.set_content('<meta charset="utf-8">' + section(shell + record('匿名甲大学', 'neighbor-owned')) +
                '<script>window.events=0;for(const type of ["click","input","change","submit"])document.addEventListener(type,()=>window.events++)</script>')
            adjacent = await service.snapshot(probe_options=False)
            shell_members = [f for f in adjacent.fields if f.record_evidence == 'ant-resume-shell-observed']
            owned_members = [f for f in adjacent.fields if f.record_evidence == 'ant-resume-owned']
            assert len(shell_members) == 4 and len(owned_members) == 16
            assert {f.container_key for f in shell_members}.isdisjoint({f.container_key for f in owned_members})
            inventory = await inspect_ant_resume_records(page)
            assert len(inventory) == 1 and inventory[0]['record_count'] == 1
            assert set(inventory[0]['record_keys']) == {f.container_key for f in owned_members}
            neighbor_resolutions = _education_resolutions(adjacent, profile)
            shell_degree = next(f for f in shell_members if f.semantic_key == 'education.degree')
            assert neighbor_resolutions[shell_degree.selector].record is None
            assert await page.evaluate('window.events') == 0

            # Recognized auxiliary questions are locally owned, never degree
            # facts or a highest-education identity assertion. A hidden native
            # transcript upload counts as its own attachment row.
            for checkbox, group in ((False, 'auxiliary'), (False, ''), (True, '')):
                extra = row('是否最高学历', group=group, checkbox=checkbox) + row('上传成绩单')
                await page.set_content('<meta charset="utf-8">' + section(record('匿名甲大学', 'owned', extra)) + '<script>window.events=0;for(const type of ["click","input","change","submit"])document.addEventListener(type,()=>window.events++)</script>')
                attached = await service.snapshot(probe_options=False)
                inventory = await inspect_ant_resume_records(page)
                assert len(inventory) == 1 and inventory[0]['record_count'] == 1
                assert len({f.container_key for f in attached.fields}) == 1
                auxiliaries = [f for f in attached.fields if f.question_text in {'是否最高学历', '上传成绩单'}]
                assert len(auxiliaries) == (2 if checkbox else 3)
                assert all(f.semantic_key == 'application.custom' and f.entity_scope == 'education:unspecified' for f in auxiliaries)
                assert all(f.record_evidence == 'ant-resume-owned' and f.section_path == ['教育经历'] for f in auxiliaries)
                assert all(f.observation.question_status == 'verified' for f in auxiliaries)
                assert next(f for f in auxiliaries if f.question_text == '上传成绩单').field_type == 'file'
                diagnostic = await inspect_ant_resume_rejections(page)
                assert diagnostic['records'][0]['verified'] and diagnostic['records'][0]['unknown_question_count'] == 0
                assert await page.evaluate('window.events') == 0

            # Merely reusing an auxiliary caption does not ignore an arbitrary
            # text input, mixed checkbox groups or a second school anchor.
            for extra in (row('上传成绩单').replace('type="file" style="display:none"', 'type="text"'),
                          row('是否最高学历').replace('class="ant-radio-group"', 'class="unowned-choice-list"'),
                          row('是否最高学历', group='owned-name').replace('name="owned-name"', 'name="other-name"', 1),
                          row('是否最高学历', group='owned-name').replace(' name="owned-name"', '', 1),
                          row('陌生题目'), row('constructor'), row('院校名称')):
                await page.set_content('<meta charset="utf-8">' + section(record('', 'rejected', extra)))
                rejected = await service.snapshot(probe_options=False)
                assert not await inspect_ant_resume_records(page)
                assert not any(f.record_evidence == 'ant-resume-owned' for f in rejected.fields)
            no_level = '<div class="resume-form-wrap">' + row('学校名称') + row('学院名称') + row('上传成绩单') + '</div>'
            await page.set_content('<meta charset="utf-8">' + section(no_level))
            assert not await inspect_ant_resume_records(page)
            assert (await inspect_ant_resume_rejections(page))['records'][0]['reason'] == 'record_attributes_missing'
            await page.set_content('<meta charset="utf-8">' + section(record('', 'false-anchor').replace(
                '<div class="field-item-wrap"><input value=""></div>',
                '<div class="field-item-wrap"><input type="checkbox"></div>', 1)))
            assert not await inspect_ant_resume_records(page)
            assert (await inspect_ant_resume_rejections(page))['records'][0]['reason'] == 'identity_control_unverified'
            # Keep all raw questions but deny record ownership for an unknown
            # mixed record or arbitrary div. No template class/heading alone
            # can turn emergency-contact details into education.
            for body in (record('', 'unknown', row('紧急联系人姓名')),
                         record('', 'duplicate', row('院校名称')),
                         record('', 'unknown').replace('class="resume-form-wrap"', 'class="unverified-record"')):
                await page.set_content('<meta charset="utf-8">' + section(body))
                rejected = await service.snapshot(probe_options=False)
                assert not await inspect_ant_resume_records(page)
                education = [f for f in rejected.fields if f.semantic_key.startswith('education.')]
                assert education and not any(f.record_evidence for f in education)
                assert all(f.observation.record_status == 'unresolved' for f in education)
        finally:
            await browser.close()
    print('ant_education_extraction_test: OK (15 questions per record, school/college/major/dates owned, no order matching or writes)')


async def run():
    # Keep planner assertions local to the anonymous profile, not the user's
    # saved application-knowledge store or a configured relay/model client.
    with patch('app.form_agent._knowledge_snapshot', side_effect=lambda snapshot: snapshot), \
         patch('app.form_agent.configured_model', side_effect=AssertionError('offline test must not call model')):
        await _run()


if __name__ == '__main__':
    asyncio.run(run())
