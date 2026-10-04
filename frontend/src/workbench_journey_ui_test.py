"""Offline UI contract test: isolated browser, synthetic profile, every request intercepted.

Run after the frontend build:
  backend/.venv/bin/python -B frontend/src/workbench_journey_ui_test.py
No backend is started and no real database, account, or recruitment site is used.
"""
import asyncio
import json
import mimetypes
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.models import CandidateProfile, Project  # noqa: E402
from app.browser_models import PageField  # noqa: E402
from playwright.async_api import async_playwright, expect  # noqa: E402


async def main():
    dist = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / 'frontend' / 'dist'
    assert (dist / 'index.html').exists(), 'Build the frontend before this isolated test'
    profile = CandidateProfile(name='匿名测试', projects=[Project(name='Synthetic Agent')]).model_dump(mode='json')
    resume = dict(id='test-cv', filename='anonymous.pdf', label='Agent 简历', profile=profile, parser='fixture',
                  status='completed', language='中文', tags=[], target_role='', is_default=True, file_size=128,
                  content_hash='fixture', error_message='', evidence=[], created_at='2026-01-01T00:00:00Z',
                  updated_at='2026-01-01T00:00:00Z')
    other_resume = {**resume, 'id': 'other-cv', 'filename': 'anonymous.docx', 'is_default': False}
    route_meta = dict(adapter='synthetic', label='隔离测试站', matched_by='dom', evidence=[], tools=[], note='')
    field = PageField(selector='#role', label='项目角色', question_text='Synthetic Agent 的项目角色',
                      field_type='text', semantic_key='project.role', entity_scope='project:Synthetic Agent',
                      container_key='project-test', required=True).model_dump(mode='json')
    state = dict(stage='auth_required', role='', journey_calls=0, capability=1, completion=1)
    writes, errors, unexpected = [], [], []
    radios = [PageField(selector='#early-'+value,label='是否可提前实习',question_text='是否可提前实习',
        field_type='radio',required=True,label_source='container-owned',current_value='false',
        option_label=label,options=['是','否'],control_group_key='early-question',
        container_key='personal',field_signature='early-signature').model_dump(mode='json')
        for value,label in [('yes','是'),('no','否')]]

    def snapshot():
        return dict(session_id='fixture-session', url='https://ats.example.invalid/application', title='隔离测试岗位',
                    recognition_profile='generic', site_route=route_meta, fields=[] if state['stage']=='auth_required' else
                    radios if state.get('radio_mode') else [{**field, 'current_value': state.get('web_role', ''),
                    **({'label':'最早可实习入职时间','question_text':'最早可实习入职时间',
                        'semantic_key':'preference.available_date','field_type':'combobox','date_precision':'date'} if state.get('date_mode') else {})}])

    def workflow():
        return dict(session_id='fixture-session', url=snapshot()['url'], title=snapshot()['title'], adapter='synthetic',
                    site_route=route_meta, stage=state['stage'], message='匿名隔离测试，不连接招聘网站', job_title='系统研发岗',
                    job_id='fixture-job', authenticated=state['stage']!='auth_required', authentication_evidence=[],
                    authentication_methods=[], requires_consent=False, verification_channel='', registration_identifiers=[],
                    registration_requires_password=False, form_fields=len(snapshot()['fields']), final_submit_present=True,
                    safe_next_present=False, safe_next_label='', page_step_current=None, page_step_total=None,
                    navigation_candidates=[], actions=[])

    def review():
        if state.get('radio_mode'):
            known=bool(state.get('early_answer'))
            return dict(snapshot=snapshot(),plan=dict(context_token='early-revision',resume_id='test-cv',
                page_summary='匿名单选题',site_type='generic',actions=[dict(selector=f['selector'],label='是否可提前实习',
                action=('check' if f['option_label']=='是' else 'skip') if known else 'ask_user',
                value=f['option_label']=='是' if known else '',confidence=1 if known else 0,
                reason='用户已确认的答案' if known else '程序证据不足，待模型分析',sensitive=False,
                user_confirmed=known,resolution_source='user' if known else 'model',needs_model=not known) for f in radios]),
                comparisons=[],summary=dict(matched=0,missing=1,conflict=0,manual_review=0,unmapped=0,option_unavailable=0))
        done = bool(state.get('web_role'))
        if state.get('date_mode'):
            return dict(snapshot=snapshot(), plan=dict(context_token='date-revision',resume_id='test-cv',
                page_summary='匿名日期题',site_type='generic',actions=[dict(selector='#role',label='最早可实习入职时间',
                action='ask_user',value='',confidence=1,reason='请提供真实最早可入职日期',sensitive=False,
                user_confirmed=False,resolution_source='user',needs_model=False)]),
                comparisons=[],summary=dict(matched=0,missing=1,conflict=0,manual_review=1,unmapped=0,option_unavailable=0))
        action = dict(selector='#role', label='项目角色', action='skip' if done else 'fill' if state['role'] else 'ask_user', value=state['role'],
                      value_source='已确认测试简历' if state['role'] else '', confidence=1, reason='缺少此项目的真实角色' if not state['role'] else '用户确认的事实',
                      sensitive=False, user_confirmed=False, resolution_source='rules' if state['role'] else 'user', needs_model=False)
        return dict(snapshot=snapshot(), plan=dict(context_token='revision-2' if state['role'] else 'revision-1', resume_id='test-cv',
                    page_summary='隔离测试表单', site_type='generic', actions=[action], missing_questions=[]),
                    comparisons=[dict(key='role', selector='#role', label='项目角色', field_type='text', required=True, options=[],
                    site_value=state.get('web_role', ''), expected_value=state['role'], value_source='', status='matched' if done else 'missing', recommendation='')],
                    summary=dict(matched=int(done), missing=int(not done), conflict=0, manual_review=0, unmapped=0, option_unavailable=0))

    def turn():
        done = bool(state.get('web_role'))
        return dict(snapshot=snapshot(), workflow=workflow(), action_taken='analyze_and_fill', review=review(), execution=None,
                    pre_submit=dict(url=snapshot()['url'], ready=done, required_total=1, filled_count=int(done),
                        required_missing=[] if done else [dict(selector='#role', label='项目角色', field_type='text')],
                        validation_errors=[], human_challenges=[], file_uploads=[], submit_labels=['提交']),
                    decision=dict(stage=state['stage'], goal='填写至人工终审', summary='匿名测试', next_action='review_before_submit' if done else 'stop',
                        next_label='人工终审' if done else '确认缺失资料', rationale='不猜测事实', blockers=[], user_questions=[] if done else ['请补充项目角色'],
                        can_execute=False, requires_user=True, risk_level='low', model_status='model', model='synthetic-model'),
                    checkpoint=dict(run_id='run-fixture', session_id='fixture-session', status='review' if done else 'waiting_user',
                        stage=state['stage'], url=snapshot()['url'], title='匿名测试', updated_at='2026-01-01T00:00:00Z', events=[]))

    def targets():
        return dict(resume_id='test-cv', revision='revision-2' if state['role'] else 'revision-1', warnings=[], records=[dict(
            record_key='project-stable-identity', section='projects', label='Synthetic Agent', attributes=[dict(key='role', label='项目角色', value=state['role'])])])

    async def intercept(route):
        request = route.request
        path = urlparse(request.url).path
        if urlparse(request.url).hostname=='fonts.googleapis.com':
            # Use system fonts offline; never fetch the external stylesheet.
            await route.fulfill(body='', content_type='text/css');return
        headers = {'access-control-allow-origin': 'http://zhida-ui.invalid', 'access-control-allow-credentials': 'true',
                   'access-control-allow-methods': 'GET,POST,PUT,OPTIONS', 'access-control-allow-headers': 'content-type'}
        if request.method=='OPTIONS':
            await route.fulfill(status=204, headers=headers);return
        if path.startswith('/api/'):
            if request.method not in ('GET', 'HEAD'):
                writes.append((path, request.post_data_json))
            if path=='/api/auth/me': body=dict(id='anonymous', email='fixture@example.invalid', display_name='匿名测试', created_at='', is_local=True)
            elif path=='/api/profile': body=profile
            elif path=='/api/resumes': body=[resume, other_resume]
            elif path in ('/api/conflicts', '/api/application-knowledge', '/api/application-knowledge/targets'): body=[]
            elif path=='/api/browser/current': body=dict(session_id='fixture-session', occupied=True, resume_id='test-cv', assistance_version=1, record_completion_version=state['completion'], journey_version=state['capability'])
            elif path.endswith('/snapshot'): body=snapshot()
            elif path.endswith('/workflow'): body=workflow()
            elif path.endswith('/fact-targets'): body=targets()
            elif path.endswith('/confirmed-fact'):
                assert request.post_data_json==dict(revision='revision-1', record_key='project-stable-identity', attribute='role', value='核心开发', confirmed=True)
                state['role']='核心开发';resume['profile']['projects'][0]['role']='核心开发';resume['updated_at']='2026-01-02T00:00:00Z';body=resume
            elif path.endswith('/review'): body=review()
            elif path=='/api/profile/application-answer':
                answer=request.post_data_json
                assert state.get('radio_mode') and answer['question']=='是否可提前实习'
                assert answer['value']=='是' and answer['field_type']=='radio' and answer['options']==['是','否']
                assert answer['resume_id']=='test-cv' and answer['source_url']==snapshot()['url']
                state['early_answer']=True;body=profile
            elif path.endswith('/journey'):
                assert request.post_data_json==dict(resume_id='test-cv', max_steps=4)
                state['journey_calls']+=1;state['stage']='application_form'
                if state['role']: state['web_role']=state['role']
                body=dict(status='ready_for_review' if state.get('web_role') else 'needs_user', message='已停在人工终审' if state.get('web_role') else '项目角色需由用户提供',
                    turn=turn(), steps=2, events=[dict(action='analyze_and_fill', stage='application_form', message='已识别申请表，未提交')])
            elif path.endswith('/assist'):
                assert request.post_data_json==dict(resume_id='test-cv',allow_site_parse=False,use_model=True,max_rounds=3,defer_government_id=True)
                body=dict(status='needs_user',message='其余资料已核对，证件号码按本人要求留空',snapshot=snapshot(),review=review(),pre_submit=turn()['pre_submit'],
                    events=[],rounds=0,record_coverage=[dict(kind='projects',label='项目经历',source_total=3,website_records=2,
                    matched_records=2,missing_names=['尚未展开的合成项目'],ambiguous=False,can_expand=True)])
            else:
                unexpected.append((request.method,path));await route.fulfill(status=404, headers=headers, json={'detail':'Unexpected test route'});return
            await route.fulfill(headers=headers, json=body);return
        file = dist / (path.lstrip('/') or 'index.html')
        if not file.is_file() or not file.is_relative_to(dist):
            unexpected.append(request.url);await route.abort();return
        await route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream')

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(viewport={'width':1280,'height':900},service_workers='block')
            page = await context.new_page()
            page.on('pageerror',lambda error: errors.append(str(error)))
            async def block_socket(socket):
                unexpected.append('WebSocket request')
                await socket.close()
            await page.route_web_socket('**/*',block_socket)
            await page.route('**/*',intercept)
            await page.goto('http://zhida-ui.invalid/')
            await page.get_by_role('button',name='投递工作台',exact=True).click()
            await expect(page.get_by_role('button',name='我已完成登录，让职达继续',exact=True)).to_be_enabled()
            choices = await page.get_by_label('本次投递使用哪份简历？').locator('option').all_text_contents()
            assert any('PDF' in option for option in choices) and any('DOCX' in option for option in choices), choices
            await page.get_by_role('button',name='我已完成登录，让职达继续',exact=True).click()
            await expect(page.get_by_role('heading',name='需要你确认缺失信息',exact=True)).to_be_visible()
            await page.get_by_role('textbox',name='Synthetic Agent 的项目角色',exact=True).fill('尚未执行的临时角色')
            await page.get_by_text('补充这份简历的真实资料（跨公司复用）',exact=True).click()
            await page.get_by_label('这条事实属于哪段经历？').select_option('project-stable-identity')
            await page.get_by_label('要补充的属性').select_option('role')
            await page.get_by_label('项目角色的真实值').fill('核心开发')
            assert not any(path.endswith('/confirmed-fact') for path,_ in writes)
            await page.get_by_role('button',name='预览本次修改',exact=True).click()
            await expect(page.get_by_text('新值：核心开发',exact=True)).to_be_visible()
            assert not any(path.endswith('/confirmed-fact') for path,_ in writes), 'Preview must never persist'
            await page.get_by_role('button',name='确认真实无误，保存到此简历',exact=True).click()
            await expect(page.get_by_text('真实资料已入库并重新核对，未执行的临时答案保留。附件未修改，也未自动填写招聘网页。',exact=True)).to_be_visible()
            assert not any(path.endswith('/execute') for path,_ in writes), 'Fact confirmation never writes the website'
            await expect(page.get_by_role('button',name='先处理补充答案',exact=True)).to_be_visible()
            page.once('dialog', lambda dialog: dialog.accept())
            await page.get_by_role('button',name='清除本页临时修改',exact=True).click()
            await page.get_by_role('button',name='让职达继续',exact=True).click()
            await expect(page.get_by_role('heading',name='已到人工终审，尚未提交',exact=True)).to_be_visible()
            assert state['journey_calls']==2 and state['web_role']=='核心开发'
            await page.screenshot(path='/private/tmp/zhida-journey-ui-isolated.png',full_page=True)

            # A read-only resync cannot silently move retained answers to another record.
            state['role']='';state['web_role']=''
            await page.reload()
            await page.get_by_role('button',name='投递工作台',exact=True).click()
            await page.get_by_role('button',name='让职达继续',exact=True).click()
            await page.get_by_role('textbox',name='Synthetic Agent 的项目角色',exact=True).fill('必须保留的原问题答案')
            before = state['journey_calls']
            field['question_text']='另一个项目的角色'
            await page.get_by_role('button',name='只读同步当前页',exact=True).click()
            await expect(page.get_by_text('已只读查看新页面，但未执行答案与当前页面或简历不兼容。答案仍保留，旧计划保持暂停；请先核对并清除不适用的临时修改，再同步。',exact=False)).to_be_visible()
            await page.get_by_text('查看保留的临时答案',exact=True).click()
            await expect(page.locator('.workbench-retained-note details').get_by_text('必须保留的原问题答案',exact=False)).to_be_visible()
            assert state['journey_calls']==before
            # A user can proactively confirm a model-pending radio question
            # using its real options and save it without an ATS write.
            state['radio_mode']=True
            await page.reload()
            await page.get_by_role('button',name='投递工作台',exact=True).click()
            await page.get_by_text('其他填写方式：只核对、仅 AI 分析或重新填写',exact=True).click()
            await page.get_by_role('button',name='只核对，不填写',exact=True).click()
            await page.get_by_text('查看逐字段处理依据与修改入口（1）',exact=True).click()
            await page.get_by_role('button',name='我来确认',exact=True).click()
            await page.get_by_label('是否可提前实习',exact=True).select_option('#early-yes')
            # The manual section appears as well and must retain the answer.
            await expect(page.get_by_label('是否可提前实习',exact=True).last).to_have_value('#early-yes')
            await page.get_by_role('button',name='记住本网站答案',exact=True).first.click()
            await expect(page.get_by_text('已记住本次简历在本网站的答案，并重新核对',exact=False)).to_be_visible()
            assert state['early_answer'] and not any(path.endswith('/execute') for path,_ in writes)
            assert sum(path=='/api/profile/application-answer' for path,_ in writes)==1
            state['radio_mode']=False
            state['date_mode']=True
            await page.reload()
            await page.get_by_role('button',name='投递工作台',exact=True).click()
            await page.get_by_text('其他填写方式：只核对、仅 AI 分析或重新填写',exact=True).click()
            await page.get_by_role('button',name='只核对，不填写',exact=True).click()
            date_input=page.get_by_label('最早可实习入职时间',exact=True)
            await expect(date_input).to_have_attribute('type','date')
            await date_input.fill('2027-02-01')
            await expect(date_input).to_have_value('2027-02-01')
            await expect(page.get_by_text('尚未读到招聘网站的真实选项',exact=False)).to_have_count(0)
            state['date_mode']=False
            # The one-click entry carries only this run's ID deferral and shows
            # missing record coverage, never claiming it has submitted.
            state['web_role']='核心开发';state['role']='核心开发'
            await page.reload()
            await page.get_by_role('button',name='投递工作台',exact=True).click()
            await page.get_by_role('checkbox',name='本次先不填证件号码',exact=False).check()
            page.once('dialog',lambda dialog:dialog.accept())
            await page.get_by_role('button',name='自动补齐并核对',exact=True).click()
            await expect(page.get_by_text('本次简历的记录覆盖',exact=True)).to_be_visible()
            await expect(page.get_by_text('尚未展开的合成项目',exact=False)).to_be_visible()
            assert sum(path.endswith('/assist') for path,_ in writes)==1
            state['completion']=0
            await page.reload()
            await page.get_by_role('button',name='投递工作台',exact=True).click()
            await expect(page.get_by_role('button',name='自动补齐并核对',exact=True)).to_be_disabled()
            state['capability']=0;state['stage']='auth_required'
            await page.reload()
            await page.get_by_role('button',name='投递工作台',exact=True).click()
            await expect(page.get_by_role('button',name='我已完成登录，让职达继续',exact=True)).to_be_disabled()
            await expect(page.get_by_role('button',name='只读同步当前页',exact=True)).to_be_enabled()
            assert not unexpected, unexpected
            assert not errors, errors
            print('workbench_journey_ui_test: OK (isolated login gate -> questions -> two-step fact confirmation -> fresh review -> journey -> human review; retained/stale drafts protected; legacy capability blocked; no real network/database)')
        finally:
            await browser.close()


if __name__=='__main__': asyncio.run(main())
