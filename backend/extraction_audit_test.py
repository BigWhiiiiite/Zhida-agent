"""No browser/model/network: whole-page audit coverage and zero-write contract."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents import ModelSettings

from app import extraction_audit as audit
from app.browser_models import BrowserSnapshot, PageField, QuestionEvidence
from app.page_observation import PageRegionObservation


def fixture(count=35):
    fields = [PageField(selector=f'#q{i}', label=f'问题{i}', question_text=f'问题{i}',
        label_source='explicit', current_value='private-answer',
        question_candidates=[QuestionEvidence(text=f'问题{i}', source='explicit', owned=True)])
        for i in range(count)]
    fields[0].label = fields[0].question_text = ''
    fields[0].label_source = 'unknown'
    fields[0].question_candidates = []
    fields[1].field_type = 'combobox'
    fields[1].options_capture = 'deferred'
    fields[2].label = fields[2].question_text = '密码'
    fields[2].field_type = 'password'
    return BrowserSnapshot(session_id='anonymous', url='https://fixture.example.test/form?postId=1',
        title='Anonymous', fields=fields)


def output_for(page):
    return audit.AuditBatch(questions=[audit.QuestionReview(question_id=q['id'],
        interpretation=q['question_text'] or '原题尚未读清楚', control_kind=q['control_kind'],
        verdict='needs_observation' if q['issues'] else 'clear', issue='；'.join(q['issues']),
        next_observation='只读核对原题' if q['issues'] else '', evidence=[q['question_text']] if q['question_text'] else [])
        for q in page['questions']])


async def run():
    source = fixture()
    fingerprint = audit.document_fingerprint(source)
    page = audit.audit_page(source)
    encoded = json.dumps(page, ensure_ascii=False)
    assert len(page['questions']) == 34 and page['questions'][0]['question_status'] == 'missing'
    assert 'private-answer' not in encoded and '"current_value"' not in encoded
    assert '#q2' not in {t['selector'] for q in page['questions'] for t in q['targets']}
    assert page['input_manifest']['credential_controls_excluded'] == 1
    otp = PageField(selector='#otp', label='', field_type='text', autocomplete='one-time-code')
    assert not audit._can_observe(otp) and not audit._can_view(otp)
    otp_page = audit.audit_page(BrowserSnapshot(session_id='anonymous',
        url='https://fixture.example.test/form', title='Anonymous', fields=[otp]))
    assert not otp_page['questions'] and otp_page['input_manifest']['credential_controls_excluded'] == 1
    aria = PageRegionObservation(selector='#truth', accessibility_source='playwright_aria',
        accessibility='- checkbox "本人承诺" [checked]\n- option "是" [selected]\n- textbox "备注: 内容": 7',
        context={'labels': ['private-answer']}, image_data_url='data:image/png;base64,YQ==')
    safe_aria = audit._audit_region(aria, ['private-answer'])
    assert '[checked]' not in safe_aria.accessibility and '[selected]' not in safe_aria.accessibility
    assert ': 7' not in safe_aria.accessibility and 'private-answer' not in safe_aria.model_dump_json()
    assert '"备注: 内容"' in safe_aria.accessibility  # colons inside a title are not values
    assert '本人承诺' in safe_aria.accessibility and safe_aria.image_data_url == aria.image_data_url
    assert '[checked]' in aria.accessibility  # no mutation of original evidence
    request = audit.ExtractionAuditRequest(context_token='x'*64)
    inputs = []
    async def fake_run(agent, messages, max_turns):
        assert not agent.tools and agent.output_type == audit.AuditBatch and max_turns == 1
        assert 'candidate_profile' not in messages[0]['content'][0]['text'].replace('candidate_profile_sent', '')
        supplied = json.loads(messages[0]['content'][0]['text'])
        inputs.extend(q['id'] for q in supplied['questions'])
        return SimpleNamespace(final_output=output_for(supplied))
    with patch.object(audit, 'configured_model', return_value=('fixture', ModelSettings())), \
         patch.object(audit.Runner, 'run', side_effect=fake_run):
        result = await audit.audit_extraction(source, request)
    assert result.model_status == 'complete' and result.model_reviewed_questions == 34
    assert result.model_batches == 3 and len(set(inputs)) == len(inputs) == 34
    assert result.questions[0].model_review.verdict == 'needs_observation'
    assert audit.document_fingerprint(source) == fingerprint
    assert 'actions' not in result.model_dump() and 'image_data_url' not in result.model_dump_json()
    private = fixture()
    private.fields[3].section_path = ['private-answer']
    private.fields[3].constraints.pattern = 'private-answer'
    private.fields[3].required_evidence = ['required private-answer']
    audit.refresh_report(private)
    private.extraction_report.limitations.append('private-answer')
    assert 'private-answer' not in json.dumps(audit.audit_page(private))
    changed = source.model_copy(deep=True)
    changed.fields[1].constraints.pattern = 'new-pattern'
    assert audit.document_fingerprint(changed) != fingerprint
    changed = source.model_copy(deep=True)
    changed.fields[1].record_evidence = 'different-owner'
    assert audit.document_fingerprint(changed) != fingerprint
    # A newly discovered unread region is a scope change even if visible
    # question selectors and values happen to remain unchanged.
    scoped = source.model_copy(deep=True)
    scoped.extraction_report = audit.build_report(scoped.fields, {
        'observed_controls': len(scoped.fields), 'captured_controls': len(scoped.fields)})
    scoped_fingerprint = audit.document_fingerprint(scoped)
    scoped.extraction_report.unread_shadow_regions = 2
    scoped.extraction_report.embedded_regions = 1
    scoped.extraction_report.pending_sections = ['教育经历']
    audit.refresh_report(scoped)
    assert audit.document_fingerprint(scoped) != scoped_fingerprint
    scoped_page = audit.audit_page(scoped)
    assert scoped_page['input_manifest']['surface_inventory_status'] == 'partial'
    assert scoped_page['input_manifest']['unread_shadow_regions'] == 2
    assert scoped_page['input_manifest']['unread_embedded_regions'] == 1
    assert scoped_page['input_manifest']['pending_sections'] == ['教育经历']

    # Failure/omission cannot be mislabeled as model success or patched locally.
    async def failed_batch(page, model_name, regions):
        if page['batch_manifest']['start'] == 16:
            raise RuntimeError('relay echoes SECRET')
        return output_for(page)
    with patch.object(audit, '_review_batch', side_effect=failed_batch):
        result = await audit.audit_extraction(source, request)
    assert result.model_status == 'partial' and result.model_reviewed_questions == 18
    assert 'SECRET' not in result.model_dump_json()
    async def missing_output(agent, messages, max_turns):
        return SimpleNamespace(final_output=audit.AuditBatch(questions=[]))
    with patch.object(audit, 'configured_model', return_value=('fixture', ModelSettings())), \
         patch.object(audit.Runner, 'run', side_effect=missing_output):
        result = await audit.audit_extraction(source, request)
    assert result.model_status == 'unavailable' and result.model_reviewed_questions == 0
    # Prompt-JSON providers still validate complete IDs and real evidence.
    async def text_run(agent, messages, max_turns):
        assert agent.output_type is None
        supplied = json.loads(messages[0]['content'][1]['text'])
        output = output_for(supplied)
        output.questions[0].verdict = 'clear'
        output.questions[0].evidence = ['fabricated quote not present']
        return SimpleNamespace(final_output=output.model_dump_json())
    with patch.dict('os.environ', {'APP_AGENT_MODEL':'fixture', 'APP_AGENT_PROMPT_JSON_MODELS':'fixture'}), \
         patch.object(audit, 'configured_model', return_value=('fixture', ModelSettings())), \
         patch.object(audit.Runner, 'run', side_effect=text_run):
        result = await audit.audit_extraction(source, request)
    assert result.model_status == 'complete'
    assert result.questions[0].model_review.verdict == 'conflict'
    assert not result.questions[0].model_review.evidence

    # Menu discovery enriches only metadata; no choices or values are written.
    observed = source.fields[1].model_copy(update={'region_picker': True,
        'options':['省A'], 'options_capture':'observed_subset'})
    controls = AsyncMock(return_value=[observed])
    region = AsyncMock(return_value=PageRegionObservation(selector='#q0',
        image_data_url='data:image/png;base64,YQ=='))
    request = request.model_copy(update={'use_model':False, 'inspect_controls':True})
    result = await audit.audit_extraction(source, request, observe_controls=controls, observe_region=region)
    controls.assert_awaited_once_with(['#q1'])
    region.assert_not_awaited()
    assert result.model_status == 'not_requested' and result.questions[1].options_status == 'dependent'
    assert not source.fields[1].region_picker
    # Legal dropdowns are audited structurally, never opened by menu probes.
    declaration = PageField(selector='#truth', question_text='本人承诺上述内容真实有效',
        label='本人承诺上述内容真实有效', field_type='combobox', options_capture='deferred')
    source.fields.append(declaration)
    controls.reset_mock()
    await audit.audit_extraction(source, request, observe_controls=controls)
    controls.assert_awaited_once_with(['#q1'])
    stale_region = AsyncMock(side_effect=ValueError('stale identity'))
    try:
        await audit.audit_extraction(source, request.model_copy(update={
            'inspect_controls':False, 'include_images':True}), observe_region=stale_region)
    except ValueError as exc:
        assert 'stale' in str(exc)
    else:
        raise AssertionError('stale visual observation swallowed')

    # Changed identity/value makes evidence unusable, never silently merges it.
    controls.return_value = [observed.model_copy(update={'current_value':'changed'})]
    try:
        await audit.audit_extraction(source, request, observe_controls=controls)
    except ValueError:
        pass
    else:
        raise AssertionError('changed answer accepted')
    print('extraction_audit_test: OK (all questions, no answers, no actions, batch coverage, stale evidence)')


if __name__ == '__main__':
    asyncio.run(run())
