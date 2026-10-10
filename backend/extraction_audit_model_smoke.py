"""Explicit manual smoke test: configured model, anonymous questions, no browser.

Never part of the offline suite. Does not read candidate/resume storage or
execute any website actions. Prints only an anonymous model-audit receipt.
"""
import asyncio
import argparse
import json
import re
from unittest.mock import patch

from app.browser_models import BrowserSnapshot, PageField, QuestionEvidence
from app.extraction_audit import AuditBatch, ExtractionAuditRequest, Runner, audit_extraction
from app.model_provider import configured_model
from app.form_observation import build_report


def anonymous_form():
    """Long anonymous form tests real-provider batching, not live ATS capture."""
    specs = [
        ('姓名', 'text', []), ('性别', 'select-one', ['男', '女']),
        ('民族', 'select-one', ['汉族', '其他']), ('身高（CM）', 'number', []),
        ('体重（KG）', 'number', []), ('出生日期', 'combobox', []),
        ('政治面貌', 'combobox', []), ('婚姻状况', 'select-one', ['已婚', '未婚']),
        ('生源地', 'combobox', []), ('户口所在地', 'combobox', []),
        ('现居住地', 'combobox', []), ('电子邮箱', 'email', []),
        ('移动电话', 'tel', []), ('微信', 'text', []), ('通信地址', 'textarea', []),
        ('紧急联系人姓名', 'text', []), ('紧急联系方式', 'tel', []),
        ('学校名称', 'text', []), ('学院名称', 'text', []),
        ('所学专业', 'text', []), ('学历', 'combobox', []),
        ('学位', 'combobox', []), ('入学日期', 'combobox', []),
        ('毕业日期', 'combobox', []), ('实习单位', 'text', []),
        ('实习岗位', 'text', []), ('实习内容', 'textarea', []),
        ('实习开始日期', 'combobox', []), ('实习结束日期', 'combobox', []),
        ('IT技能', 'textarea', []), ('校内奖励', 'textarea', []),
        ('本人承诺上述内容真实有效', 'combobox', []), ('简历附件', 'file', []),
        ('是', 'radio', []),
    ]
    fields = [PageField(selector=f'#anonymous-{i}', label=title, question_text=title,
        label_source='explicit' if title != '是' else 'unknown', field_type=kind,
        control_kind='calendar' if '日期' in title else '',
        region_picker=title in {'生源地', '户口所在地', '现居住地'},
        options=options, options_capture='native_complete' if kind == 'select-one' else '')
        for i, (title, kind, options) in enumerate(specs)]
    semantic_keys = {'学校名称':'education.school', '学院名称':'education.college',
        '所学专业':'education.major', '学历':'education.degree', '学位':'education.degree',
        '入学日期':'education.start_date', '毕业日期':'education.end_date',
        '实习单位':'experience.organization', '实习岗位':'experience.position',
        '实习内容':'experience.description', '实习开始日期':'experience.start_date',
        '实习结束日期':'experience.end_date'}
    for field in fields:
        field.semantic_key = semantic_keys.get(field.label, '')
        if field.label != '是':
            field.question_candidates = [QuestionEvidence(text=field.label, source='explicit', owned=True)]
    snapshot = BrowserSnapshot(session_id='anonymous-full-model-smoke',
        url='https://fixture.example.test/form', title='Anonymous long-form extraction audit', fields=fields)
    snapshot.extraction_report = build_report(fields, {
        'observed_controls': len(fields), 'captured_controls': len(fields),
        'embedded_regions': 1, 'unread_shadow_regions': 1,
        'pending_sections': ['教育经历']})
    return snapshot


async def run(full_page=False):
    configured_model()  # load current configured provider, never print credentials
    snapshot = anonymous_form() if full_page else BrowserSnapshot(session_id='anonymous-model-smoke',
        url='https://fixture.example.test/form', title='Anonymous extraction audit', fields=[
            PageField(selector='#name', label='姓名', question_text='姓名', label_source='explicit'),
            PageField(selector='#calendar', label='出生日期', question_text='出生日期',
                      label_source='explicit', field_type='combobox', control_kind='calendar'),
            PageField(selector='#unclear', label='是', question_text='是', field_type='radio'),
        ])
    # Capture pre-validation model evidence ONLY for this anonymous test. The
    # production receipt never retains rejected quotes or model input bytes.
    raw_reviews = {}
    original_run = Runner.run
    async def capture(*args, **kwargs):
        response = await original_run(*args, **kwargs)
        raw = response.final_output
        if isinstance(raw, str):
            body = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip()).strip()
            try:
                raw = AuditBatch.model_validate_json(body)
            except ValueError:
                raw = None
        if isinstance(raw, AuditBatch):
            for question in raw.questions:
                raw_reviews[question.question_id] = question.model_dump()
        return response
    with patch.object(Runner, 'run', side_effect=capture):
        result = await audit_extraction(snapshot, ExtractionAuditRequest(context_token='x'*64))
    print(json.dumps({'model_status':result.model_status, 'model_name':result.model_name,
        'questions_sent':result.total_questions, 'questions_reviewed':result.model_reviewed_questions,
        'model_batches':result.model_batches, 'capture_status':result.coverage.capture_status,
        'input_manifest':result.input_manifest,
        'browser_used':False, 'personal_data_used':False,
        'questions':[{'title':q.title, 'issues':q.issues,
            'verdict':q.model_review.verdict if q.model_review else 'unreviewed',
            'model_issue':q.model_review.issue if q.model_review else '',
            'evidence':q.model_review.evidence if q.model_review else [],
            'raw_model_review':raw_reviews.get(q.question_id)} for q in result.questions]},
        ensure_ascii=False))
    if result.model_status != 'complete':
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--full-page', action='store_true',
        help='Exercise real-model batching with an anonymous long form, not a live recruitment page.')
    asyncio.run(run(parser.parse_args().full_page))
