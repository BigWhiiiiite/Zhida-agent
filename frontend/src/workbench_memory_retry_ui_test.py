"""Anonymous offline UI regression cases; every HTTP request is intercepted.

Pass an isolated frontend build directory as the first argument. No live server,
existing browser profile, recruitment site, database, or personal fixture is used.
"""
import asyncio
import mimetypes
import sys
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'backend'))
from app.models import CandidateProfile, Project  # noqa: E402
from app.browser_models import PageField  # noqa: E402
from playwright.async_api import async_playwright, expect  # noqa: E402


async def case(browser, dist, scenario):
    profile = CandidateProfile(name='匿名测试', projects=[Project(name='Synthetic Project')]).model_dump(mode='json')
    resume = dict(id='test-cv', filename='anonymous.pdf', label='匿名简历', profile=profile, parser='fixture',
                  status='completed', language='中文', tags=[], target_role='', is_default=True, file_size=1,
                  content_hash='fixture', error_message='', evidence=[], created_at='', updated_at='')
    field = PageField(selector='#role', name='role', label='项目角色', question_text='Synthetic Project 的项目角色',
                      field_type='text', semantic_key='project.role', entity_scope='project:synthetic',
                      container_key='project-test', required=not (scenario.startswith('validated_skip') or scenario == 'skipped_model_display')).model_dump(mode='json')
    state = dict(resume_id='test-cv', memory_fail=True, review_fail=False, targets_fail=False, role='', web='')
    writes, errors, unexpected = [], [], []
    route_meta = dict(adapter='synthetic', label='隔离测试', matched_by='dom', evidence=[], tools=[], note='')

    def snapshot():
        return dict(session_id='fixture-session', url='https://ats.example.invalid/application', title='隔离表单',
                    recognition_profile='generic', site_route=route_meta, fields=[{**field, 'current_value':state['web']}])

    def workflow():
        return dict(session_id='fixture-session', url=snapshot()['url'], title='隔离表单', adapter='synthetic',
                    site_route=route_meta, stage='application_form', message='匿名测试', job_title='测试岗', job_id='fixture-job',
                    authenticated=True, authentication_evidence=[], authentication_methods=[], requires_consent=False,
                    verification_channel='', registration_identifiers=[], registration_requires_password=False,
                    form_fields=1, final_submit_present=True, safe_next_present=True, safe_next_label='下一页',
                    page_step_current=None, page_step_total=None, navigation_candidates=[], actions=[])

    def preflight():
        return dict(url=snapshot()['url'], ready=bool(state['web']) or not field['required'], required_total=int(field['required']), filled_count=int(bool(state['web'])),
                    required_missing=[], validation_errors=[], human_challenges=[], file_uploads=[], submit_labels=['提交'])

    def review():
        return dict(snapshot=snapshot(), plan=dict(context_token='fixture-token', resume_id=state['resume_id'],
                    page_summary='测试', site_type='generic', missing_questions=[],
                    routing_summary=dict(rules_ready=0, model_resolved=0,
                        model_pending=int(scenario == 'skipped_model_display'), needs_user=int(scenario != 'skipped_model_display')),
                    actions=[dict(selector='#role',
                    label='项目角色', action='ask_user', value='', value_source='', confidence=1, reason='请确认',
                    sensitive=False, user_confirmed=False, resolution_source='user', needs_model=scenario == 'skipped_model_display')]),
                    comparisons=[], summary=dict(matched=0, missing=1, conflict=0, manual_review=0, unmapped=0, option_unavailable=0))

    def turn():
        return dict(snapshot=snapshot(), workflow=workflow(), action_taken='analyze_and_fill', review=review(), execution=None,
                    pre_submit=preflight(), decision=dict(stage='application_form', goal='测试', summary='测试',
                    next_action='stop', next_label='确认资料', rationale='测试', blockers=[], user_questions=['确认项目角色'],
                    can_execute=False, requires_user=True, risk_level='low', model_status='model', model='synthetic'),
                    checkpoint=dict(run_id='fixture-run', session_id='fixture-session', status='waiting_user',
                    stage='application_form', url=snapshot()['url'], title='测试', updated_at='', events=[]))

    async def intercept(route):
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if parsed.hostname == 'fonts.googleapis.com':
            await route.fulfill(body='', content_type='text/css'); return
        headers = {'access-control-allow-origin':'http://zhida-ui.invalid', 'access-control-allow-credentials':'true',
                   'access-control-allow-methods':'GET,POST,PUT,OPTIONS', 'access-control-allow-headers':'content-type'}
        if request.method == 'OPTIONS':
            await route.fulfill(status=204, headers=headers); return
        if parsed.hostname == 'zhida-ui.invalid' and path.startswith('/api/'):
            if request.method not in ('GET', 'HEAD'):
                writes.append((path, request.post_data_json))
            failure = None
            if path == '/api/auth/me': body = dict(id='anonymous', email='fixture@example.invalid', display_name='匿名', created_at='', is_local=True)
            elif path == '/api/profile': body = profile
            elif path == '/api/resumes': body = [resume, {**resume, 'id':'other-cv', 'is_default':False}]
            elif path in ('/api/conflicts', '/api/application-knowledge', '/api/application-knowledge/targets'): body = []
            elif path == '/api/browser/current': body = dict(session_id='fixture-session', occupied=True, resume_id=state['resume_id'], assistance_version=1, journey_version=1)
            elif path.endswith('/snapshot'): body = snapshot()
            elif path.endswith('/workflow'): body = workflow()
            elif path.endswith('/review'):
                body = review()
                if state['review_fail']: failure = 'synthetic review refresh failure'
            elif path.endswith('/journey'):
                body = dict(status='needs_user', message='需确认', turn=turn(), steps=1, events=[])
            elif path.endswith('/execute'):
                skipped = request.post_data_json['actions'][0]['action'] == 'skip'
                if not skipped: state['web'] = request.post_data_json['actions'][0]['value']
                body = dict(verified=int(not skipped), failed=0, skipped=int(skipped), results=[dict(selector='#role', label='项目角色',
                    status='skipped' if skipped else 'filled', value=state['web'], verified=not skipped, message='已核对')], pre_submit=preflight())
            elif path.endswith('/check'): body = preflight()
            elif path.endswith('/workflow/advance'):
                assert request.post_data_json['intent'] == 'continue_application'
                body = workflow()
            elif path == '/api/profile/application-answer':
                body = profile
                if state['memory_fail']: failure = 'synthetic memory write failure'
            elif path.endswith('/fact-targets'):
                body = dict(resume_id='test-cv', revision='v1', warnings=[], records=[dict(record_key='synthetic-project',
                    section='projects', label='Synthetic Project', attributes=[dict(key='role', label='项目角色', value=state['role'])])])
                if state['targets_fail']: failure = 'synthetic editable targets failure'
            elif path.endswith('/confirmed-fact'):
                state['role'] = request.post_data_json['value']
                resume['profile']['projects'][0]['role'] = state['role']
                if scenario == 'fact_targets_failure': state['targets_fail'] = True
                body = resume
            else:
                unexpected.append((request.method, request.url)); await route.abort(); return
            await route.fulfill(status=503 if failure else 200, headers=headers,
                                json={'detail':failure} if failure else body); return
        if parsed.hostname != 'zhida-ui.invalid' or parsed.port is not None:
            unexpected.append(request.url); await route.abort(); return
        file = (dist / (path.lstrip('/') or 'index.html')).resolve()
        if not file.is_relative_to(dist) or not file.is_file():
            unexpected.append(request.url); await route.abort(); return
        await route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or 'application/octet-stream')

    context = await browser.new_context(viewport={'width':1280,'height':900}, service_workers='block')
    page = await context.new_page()
    page.set_default_timeout(2500)
    page.on('pageerror', lambda error: errors.append(str(error)))
    async def block_socket(socket):
        unexpected.append('WebSocket'); await socket.close()
    await page.route_web_socket('**/*', block_socket)
    await page.route('**/*', intercept)
    try:
        await page.goto('http://zhida-ui.invalid/')
        await page.get_by_role('button', name='投递工作台', exact=True).click()
        await page.get_by_role('button', name='让职达继续', exact=True).click()
        if scenario == 'skipped_model_display':
            await page.locator('.routing-actions > summary').click()
            row = page.locator('.routing-actions article').first
            await expect(row).to_contain_text('待模型分析')
            await row.get_by_role('button', name='不填写', exact=True).click()
            await expect(row).to_contain_text('本次留空')
            await expect(row).not_to_contain_text('待分析')
            await expect(row).not_to_contain_text('交给模型')
            await expect(page.locator('.autofill-routing')).to_contain_text('0 项待分析')
            await expect(page.locator('.plan-summary')).to_contain_text('1 项本次留空（不代表必填检查通过）')
            assert not any(path.endswith('/execute') or path.endswith('/workflow/advance') for path,_ in writes)
            assert not errors, errors
            assert not unexpected, unexpected
            return
        answer = page.get_by_role('textbox', name='Synthetic Project 的项目角色', exact=True)
        await answer.fill('原角色')
        if scenario.startswith('validated_skip'):
            await answer.fill('')
            await page.locator('.routing-actions > summary').click()
            await page.locator('.routing-actions').get_by_role('button', name='不填写', exact=True).click()
            next_button = page.get_by_role('button', name='检查通过，进入“下一页”', exact=True)
            await expect(next_button).to_be_disabled()
            if scenario == 'validated_skip_check':
                await page.get_by_role('button', name='重新检查', exact=True).click()
            else:
                await page.get_by_role('button', name='填写并验证 0 个字段', exact=True).click()
            await expect(next_button).to_be_enabled()
            await expect(page.locator('.routing-actions').get_by_role('button', name='恢复', exact=True)).to_be_enabled()
            before_journeys = len([path for path,_ in writes if path.endswith('/journey')])
            await page.get_by_role('button', name='先处理补充答案', exact=True).click()
            assert len([path for path,_ in writes if path.endswith('/journey')]) == before_journeys, 'Automatic journey must not consume local skip consent'
            if scenario == 'validated_skip_toggle':
                await page.locator('.routing-actions').get_by_role('button', name='恢复', exact=True).click()
                await page.locator('.routing-actions').get_by_role('button', name='不填写', exact=True).click()
                await expect(next_button).to_be_disabled()
            else:
                if scenario == 'validated_skip_changed': field['question_text'] = '另一个项目的角色'
                await next_button.click()
                if scenario == 'validated_skip_changed':
                    await expect(page.locator('.workbench-next h2')).to_contain_text('重新识别')
                    assert not any(path.endswith('/workflow/advance') for path,_ in writes)
                else:
                    await expect(page.locator('.notice')).to_contain_text('已安全进入下一页')
                    assert len([path for path,_ in writes if path.endswith('/workflow/advance')]) == 1
            assert not any(path == '/api/profile/application-answer' for path,_ in writes), 'Skipped values must never be learned'
        elif scenario.startswith('fact_'):
            await page.get_by_text('补充这份简历的真实资料（跨公司复用）', exact=True).click()
            await page.get_by_label('这条事实属于哪段经历？').select_option('synthetic-project')
            await page.get_by_label('要补充的属性').select_option('role')
            await page.get_by_label('项目角色的真实值').fill('已确认角色')
            await page.get_by_role('button', name='预览本次修改', exact=True).click()
            if scenario == 'fact_review_failure': state['review_fail'] = True
            await page.get_by_role('button', name='确认真实无误，保存到此简历', exact=True).click()
            await expect(page.locator('.fact-notice')).to_contain_text('保存成功' if scenario == 'fact_review_failure' else '最新可编辑资料读取失败')
            assert state['role'] == '已确认角色'
            assert len([p for p,_ in writes if p.endswith('/confirmed-fact')]) == 1
            if scenario == 'fact_review_failure':
                await expect(page.get_by_role('button', name='填写并验证', exact=False)).to_have_count(0)
                await page.get_by_text('查看保留的临时答案', exact=True).click()
                await expect(page.locator('.workbench-retained-note details')).to_contain_text('原角色')
        else:
            await page.get_by_role('button', name='填写并验证 1 个字段', exact=True).click()
            retry = page.locator('.workbench-retained-note').get_by_role('button', name='记住本网站答案', exact=True)
            await expect(retry).to_be_enabled()
            assert len([p for p,_ in writes if p.endswith('/execute')]) == 1
            state['memory_fail'] = False
            if scenario == 'next_with_draft':
                await answer.fill('尚未填写的新角色')
                await expect(page.get_by_role('button', name='检查通过，进入“下一页”', exact=True)).to_be_disabled()
                await expect(answer).to_have_value('尚未填写的新角色')
            elif scenario in ('next_with_skip', 'skip_sync'):
                await page.locator('.routing-actions > summary').click()
                await page.locator('.routing-actions').get_by_role('button', name='不填写', exact=True).click()
                if scenario == 'next_with_skip':
                    await expect(page.get_by_role('button', name='检查通过，进入“下一页”', exact=True)).to_be_disabled()
                else:
                    await page.get_by_role('button', name='只读同步当前页', exact=True).click()
                    await expect(page.get_by_role('button', name='只读同步当前页', exact=True)).to_be_enabled()
                    await page.locator('.routing-actions > summary').click()
                    await expect(page.locator('.routing-actions').get_by_role('button', name='恢复', exact=True)).to_be_enabled()
                    assert not any(path.endswith('/workflow/advance') for path,_ in writes)
            elif scenario == 'edited_draft':
                await answer.fill('尚未填写的新角色')
                await page.locator('.answer-list').get_by_role('button', name='记住本网站答案', exact=True).click()
                await expect(page.locator('.notice')).to_contain_text('已记住')
                await expect(answer).to_have_value('尚未填写的新角色')
            elif scenario == 'discard_failure':
                page.once('dialog', lambda dialog: dialog.accept())
                await page.get_by_role('button', name='清除本页临时修改', exact=True).click()
                await expect(retry).to_have_count(0)
            else:
                if scenario == 'question_change': field['question_text'] = '另一个项目的角色'
                if scenario == 'options_change': field['options'] = ['开发', '测试']
                if scenario == 'scope_change': field['entity_scope'] = 'project:other'
                if scenario == 'resume_change': state['resume_id'] = 'other-cv'
                if scenario == 'retry_review_failure': state['review_fail'] = True
                await retry.click()
                if scenario.endswith('_change'):
                    await expect(page.locator('.workbench-next h2')).to_contain_text('重新识别')
                    assert len([p for p,_ in writes if p == '/api/profile/application-answer']) == 1, 'Changed context must not persist a stale answer'
                    await page.get_by_text('查看保留的临时答案', exact=True).click()
                    await expect(page.locator('.workbench-retained-note details')).to_contain_text('原角色')
                elif scenario == 'retry_review_failure':
                    await expect(page.locator('.notice')).to_contain_text('已保存')
                    await expect(page.get_by_role('button', name='填写并验证', exact=False)).to_have_count(0)
                else:
                    await expect(retry).to_have_count(0)
                    await expect(answer).to_have_value('')
                assert len([p for p,_ in writes if p.endswith('/execute')]) == 1, 'Memory retry must never repeat website writes'
            assert all(payload['resume_id'] == 'test-cv' for path,payload in writes if path == '/api/profile/application-answer')
        assert not errors, errors
        assert not unexpected, unexpected
    finally:
        await context.close()


async def main():
    dist = Path(sys.argv[1]).resolve()
    failures = []
    scenarios = sys.argv[2:] or ['retry_success', 'edited_draft', 'discard_failure', 'question_change', 'options_change',
        'scope_change', 'resume_change', 'retry_review_failure', 'fact_review_failure', 'fact_targets_failure',
        'next_with_draft', 'next_with_skip', 'skip_sync', 'validated_skip_execute', 'validated_skip_check',
        'validated_skip_toggle', 'validated_skip_changed', 'skipped_model_display']
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(channel='chrome', headless=True)
        try:
            for scenario in scenarios:
                try:
                    await case(browser, dist, scenario)
                    print(f'PASS {scenario}', flush=True)
                except Exception as error:
                    failures.append(scenario)
                    print(f'FAIL {scenario}: {error}', flush=True)
        finally:
            await browser.close()
    assert not failures, f'Failed scenarios: {failures}'
    print('workbench_memory_retry_ui_test: OK; fresh headless browser, anonymous data, all HTTP mocked, no real network')


if __name__ == '__main__': asyncio.run(main())
