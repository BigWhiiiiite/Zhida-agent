from app.browser_models import PageField
from app.form_agent import _education_option_value
from app.field_semantics import semantic_key_for

observed = PageField(selector='#observed', label='是否统招', section='教育经历', entity_scope='education:bachelor', options=['是', '否'], field_type='combobox')
assert semantic_key_for(observed) == 'education.study_mode'
from app.browser_models import BrowserSnapshot
from app.models import CandidateProfile, Education
from app.form_agent import create_local_form_plan
plan = create_local_form_plan(BrowserSnapshot(session_id='test', url='https://example.test', title='test', fields=[observed]), CandidateProfile(education=[Education(school='示例大学', degree='本科', study_mode='统招')]))
assert plan.actions[0].action == 'select' and plan.actions[0].value == '是', plan.actions[0]

study = PageField(selector='#study', label='是否统招', semantic_key='education.study_mode', options=['是', '否'])
assert _education_option_value(study, '统招') == '是'
assert _education_option_value(study, '非统招') == '否'
assert _education_option_value(study, '全日制') == '全日制'  # Not equivalent.
other = study.model_copy(update={'label': '是否接受调剂'})
assert _education_option_value(other, '统招') == '统招'
rank = PageField(selector='#rank', label='专业排名', semantic_key='education.ranking', options=['前20%', '前50%', '其他'])
assert _education_option_value(rank, '专业前20%') == '前20%'
assert _education_option_value(rank, '专业前30%') == '专业前30%'
assert _education_option_value(rank, '前50%') != '前20%'
assert _education_option_value(rank, '专业第一') == '专业第一'
print('education_option_regression_test: OK (exact yes/no and same ranking, no inferred study mode or improved rank)')
