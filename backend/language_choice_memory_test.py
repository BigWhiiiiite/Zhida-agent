"""Confirmed language choices: isolated owner DB, no browser/model/network."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import storage
from app.models import ResumeProfile
from app.profile_service import save_application_answer
from app.task_profile import compose_task_profile
from app.application_knowledge import company_scope
from app.browser_models import PageField
from app.form_agent import _saved_answer_match
from app.application_assist import _language_allocation
from app.browser_models import BrowserSnapshot


def run():
    with TemporaryDirectory(prefix='zhida-language-fixture-') as temp, \
            patch.object(storage,'DATA_DIR',Path(temp)),patch.object(storage,'DB_PATH',Path(temp)/'db.sqlite3'):
        storage.initialize()
        token=storage.set_current_user('owner')
        try:
            storage.create_resume('cv','synthetic.pdf','synthetic.pdf','cv',ResumeProfile(languages=['IELTS：6.5']),
                                  'fixture','中文',1,'hash','synthetic',[])
            options=['入门','熟练','精通','母语']
            save_application_answer('语言能力：掌握程度','','精通',semantic_key='language.proficiency',
                entity_scope='language:英语',options=options,source_url='https://a.example/form',resume_id='cv',field_type='combobox')
            resume=storage.get_resume('cv')
            assert resume.profile.languages==['IELTS：6.5']
            assert storage.get_profile().languages==[]
            scope=company_scope('https://a.example/form')
            profile=compose_task_profile(storage.get_profile(),resume,scope)
            assert len(profile.application_answer_memory)==1
            f=PageField(selector='#level',label='语言能力：掌握程度',question_text='语言能力：掌握程度',
                semantic_key='language.proficiency',entity_scope='language:英语',field_type='combobox',options=options)
            assert _saved_answer_match(f,profile).value=='精通'
            assert not _saved_answer_match(f.model_copy(update={'entity_scope':'language:日语'}),profile).value
            assert not _saved_answer_match(f.model_copy(update={'entity_scope':'language:unspecified'}),profile).value
            assert not _saved_answer_match(f.model_copy(update={'options':['一般','良好']}),profile).value
            save_application_answer('语言类型','','英语',semantic_key='language.name',entity_scope='language:英语',
                options=['英语','日语'],source_url='https://a.example/form',resume_id='cv',field_type='combobox')
            profile=compose_task_profile(storage.get_profile(),resume,scope)
            name=PageField(selector='#type',container_key='language-one',section='语言能力',label='语言类型',
                question_text='语言类型',semantic_key='language.name',entity_scope='language:unspecified',
                field_type='combobox',options=['英语','日语'])
            empty=BrowserSnapshot(session_id='fixture',url='https://a.example/form',title='fixture',fields=[name])
            assigned=_language_allocation(empty,profile)
            assert assigned and assigned.value=='英语'
            save_application_answer('语言类型','','英语',semantic_key='language.name',entity_scope='language:unspecified',
                field_signature='old-empty-slot',options=['英语','日语'],source_url='https://a.example/form',resume_id='cv',field_type='combobox')
            name.field_signature='old-empty-slot'
            profile=compose_task_profile(storage.get_profile(),resume,scope)
            assert _language_allocation(empty,profile).value=='英语'
            assert _language_allocation(empty,compose_task_profile(storage.get_profile(),resume,'other-scope')) is None
            # Changed options, ambiguous languages, partially filled groups and
            # two empty slots are not an invitation to guess.
            assert _language_allocation(empty.model_copy(update={'fields':[name.model_copy(update={'options':['英语','法语']})]}),profile) is None
            assert _language_allocation(empty.model_copy(update={'fields':[name,name.model_copy(update={'selector':'#other','container_key':'two'})]}),profile) is None
            filled=f.model_copy(update={'container_key':'language-one','current_value':'精通'})
            assert _language_allocation(empty.model_copy(update={'fields':[name,filled]}),profile) is None
            save_application_answer('语言类型','','日语',semantic_key='language.name',entity_scope='language:日语',
                options=['英语','日语'],source_url='https://a.example/form',resume_id='cv',field_type='combobox')
            assert _language_allocation(empty,compose_task_profile(storage.get_profile(),resume,scope)) is None
            other_cv=storage.create_resume('other','synthetic.pdf','other.pdf','other',ResumeProfile(languages=['英语']),
                                          'fixture','中文',1,'hash2','synthetic',[])
            assert not _saved_answer_match(f,compose_task_profile(storage.get_profile(),other_cv,scope)).value
            other=storage.set_current_user('stranger')
            try:
                assert storage.get_resume('cv') is None
                assert storage.get_profile().application_answer_memory==[]
            finally:
                storage.reset_current_user(other)
        finally:
            storage.reset_current_user(token)
    print('language_choice_memory_test: OK (language, options, CV, owner isolation; IELTS unchanged)')


if __name__=='__main__':
    run()
