"""Confirmed bank profile facts: isolated storage, no user data/model/browser."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pydantic import ValidationError

from app import storage
from app.browser_models import BrowserSnapshot, PageField
from app.field_semantics import semantic_key_for
from app.form_agent import _direct_profile_value, create_local_form_plan
from app.models import CandidateProfile, ResumeProfile
from app.profile_service import save_application_answer
from app.task_profile import compose_task_profile


def field(label, **kwargs):
    return PageField(selector='#fixture', label=label, question_text=label,
                     label_source='explicit', **kwargs)


def run():
    p = CandidateProfile(height_cm=180, weight_kg=75, student_origin='北京市/西城区',
        hometown='辽宁省/本溪市/明山区', hukou_location='北京市/西城区',
        location='北京市/海淀区', study_continuity='是', formal_employment_status='无',
        veteran_status='否')
    for question, expected, key in (
        ('*身高（CM）', '180', 'candidate.height_cm'),
        ('体重（KG）', '75', 'candidate.weight_kg'),
        ('生源地（高考所在地）', p.student_origin, 'candidate.student_origin'),
        ('户口所在地', p.hukou_location, 'candidate.hukou_location'),
        ('现居住地', p.location, 'candidate.current_location'),
        ('籍贯', p.hometown, 'candidate.hometown'),
        ('学习时间是否连续', '是', 'candidate.study_continuity'),
    ):
        f = field(question)
        assert semantic_key_for(f) == key, question
        assert _direct_profile_value(f, p)[0] == expected, question
    for label, key in [('Height (feet)', 'candidate.height_cm'), ('Weight (lb)', 'candidate.weight_kg')]:
        assert not _direct_profile_value(field(label, semantic_key=key), p)[0]
        assert not _direct_profile_value(field(label, semantic_key=key,
            knowledge_profile_path=key.removeprefix('candidate.')), p)[0]
    for name, value in [('height_cm', -1), ('weight_kg', 0), ('height_cm', float('nan'))]:
        try:
            ResumeProfile.model_validate({name: value})
            raise AssertionError('Invalid numeric fact accepted')
        except ValidationError:
            pass
    # Data remains unknown by default; no negative decision inferred from CV.
    unknown = CandidateProfile()
    assert unknown.height_cm is None and not unknown.student_origin
    assert not unknown.formal_employment_status and not unknown.study_continuity
    assert compose_task_profile(p, None).student_origin == p.student_origin
    # Sensitive veteran state is retained for review, not auto-confirmed.
    plan = create_local_form_plan(BrowserSnapshot(session_id='s', url='https://fixture.test/form',
        title='合成表单', fields=[field('是否为退役军人', field_type='combobox', options=['是', '否'])]), p)
    assert plan.actions[0].action == 'ask_user' and not plan.actions[0].user_confirmed
    with TemporaryDirectory() as directory, patch.object(storage, 'DATA_DIR', Path(directory)), \
            patch.object(storage, 'DB_PATH', Path(directory)/'fixture.db'):
        storage.initialize()
        storage.activate_local_user()
        token = storage.set_current_user(storage.LOCAL_USER_ID)
        try:
            storage.save_profile(p)
            saved = storage.get_profile()
            assert saved.student_origin == p.student_origin and saved.height_cm == 180
            assert saved.hometown != saved.hukou_location != saved.location
            save_application_answer('身高（CM）', '', '181', semantic_key='candidate.height_cm')
            assert storage.get_profile().height_cm == 181
            save_application_answer('生源地', '', '上海市/徐汇区', semantic_key='candidate.student_origin')
            saved = storage.get_profile()
            assert saved.student_origin == '上海市/徐汇区'
            assert saved.hukou_location == p.hukou_location and saved.location == p.location
        finally:
            storage.reset_current_user(token)
    print('candidate_facts_test: OK (separate identity facts, persistence, numeric units, no inferred negatives/sensitive consent)')


if __name__ == '__main__':
    run()
