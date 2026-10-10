"""Anonymous, network-isolated observed-template recognition regression."""
import asyncio
from unittest.mock import patch

from playwright.async_api import async_playwright

from app.ant_resume_records import inspect_ant_resume_records, inspect_ant_resume_rejections
from app.application_assist import _allocation, prepare_application
from app.browser_models import ApplicationAssistRequest
from app.browser_service import BrowserDemoService
from app.field_semantics import semantic_key_for
from app.form_agent import create_local_form_plan
from app.execution_safety import ExecutionTargetChanged
from app.models import CandidateProfile, Experience
from app.repeated_records import resolve_repeated_records


def row(label, value='', calendar=False, observed=False):
    control=(f'<div class="ant-picker"><div class="ant-picker-input"><input value="{value}"></div></div>'
             if calendar else f'<input class="ant-input" value="{value}"'+
             (' required' if label=='实习单位' else '')+'>')
    if observed:
        return ('<div class="resume-form-item"><div class="resume-form-title">'+label+'</div>'
            '<div class="field-input-style"><div class="ant-row ant-form-item">'
            '<div class="ant-form-item-control"><div class="field-item-wrap">'+control+
            '</div></div></div></div></div>')
    return ('<div class="resume-form-item"><div class="field-input-style"><div class="ant-form-item">'
            f'<div class="ant-form-item-label"><label>{label}</label></div>'
            f'<div class="ant-form-item-control"><div class="field-item-wrap">{control}</div></div>'
            '</div></div></div>')


def record(name='', role='', description='', duplicate=False, observed=False):
    rows=row('开始日期',calendar=True,observed=observed)+row('结束日期',calendar=True,observed=observed)
    rows+=row('实习单位',name,observed=observed)+row('实习岗位',role,observed=observed)+row('实习内容',description,observed=observed)
    if duplicate:
        rows+=row('实习单位','另一个单位')
    return '<div class="resume-form-wrap">'+rows+'</div>'


def section(records, title='学生实践经验', wrapped=False, observed=False):
    delete='<div class="delete-item" onclick="window.deletes=(window.deletes||0)+1">删除</div>' if observed else ''
    body='<div>'+records+delete+'</div>' if wrapped else records
    return '<div class="resume-tpl-wrap"><div class="section-title">'+title+'<button>+ 添加</button></div>'+body+'</div>'


async def _run():
    for question,key in (('实习单位','organization'),('实习岗位','role'),('实习内容','description')):
        from app.browser_models import PageField
        assert semantic_key_for(PageField(selector='#test',label=question,question_text=question))=='experience.'+key
    profile=CandidateProfile(internships=[
        Experience(organization='合成单位甲',role='合成岗位甲',description='仅甲的实习职责'),
        Experience(organization='合成单位乙',role='合成岗位乙',description='仅乙的实习职责')])
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,channel='chrome')
        try:
            context=await browser.new_context(service_workers='block')
            await context.route('**/*',lambda route:route.abort())
            await context.route_web_socket('**/*',lambda socket:socket.close())
            page=await context.new_page()
            service=BrowserDemoService()
            service.page,service.session_id=page,'anonymous-records'
            await page.set_content('<meta charset="utf-8">'+section(record('合成单位乙',observed=True)+record('合成单位甲',observed=True),wrapped=True,observed=True)+
                '<script>window.clicks=0;document.addEventListener("click",()=>window.clicks++);</script>')
            first=await service.snapshot()
            inventory=await inspect_ant_resume_records(page)
            assert len(inventory)==1 and inventory[0]['record_count']==2
            assert not inventory[0]['selector']  # no guessed add-button action
            diagnostic=await inspect_ant_resume_rejections(page)
            assert all(r['reason']=='verified' and r['row_count']==5 for r in diagnostic['records'])
            assert '合成单位' not in str(diagnostic)  # values never appear in diagnostics
            anchors=[f for f in first.fields if f.semantic_key=='experience.organization']
            assert len(anchors)==2 and len({f.container_key for f in anchors})==2
            assert all(f.required and f.required_evidence for f in anchors)
            for anchor in anchors:
                members=[f for f in first.fields if f.container_key==anchor.container_key]
                assert len(members)==5 and {f.section for f in members}=={'学生实践经验'}
                assert {f.semantic_key for f in members}=={'experience.'+key for key in
                    ('start_date','end_date','organization','role','description')}
            bindings=resolve_repeated_records(first,profile)
            # Reverse DOM order must still bind every role/description to its
            # actual organization, never the source's list position.
            with patch('app.form_agent.configured_model',return_value='offline'):
                plan=create_local_form_plan(first,profile)
            for anchor in anchors:
                source=next(item for item in profile.internships if item.organization==anchor.current_value)
                members=[f for f in first.fields if f.container_key==anchor.container_key]
                assert all(bindings[f.selector][1] is source for f in members)
                for field in members:
                    if field.semantic_key in {'experience.role','experience.description'}:
                        action=next(a for a in plan.actions if a.selector==field.selector)
                        assert action.value==getattr(source,field.semantic_key.split('.')[1])
            second=await service.snapshot()
            assert {f.container_key for f in second.fields}=={f.container_key for f in first.fields}
            assert await page.evaluate('window.clicks')==0

            # A validated empty slot can be explicitly allocated, but the
            # resolver itself may not guess between two source experiences.
            await page.set_content('<meta charset="utf-8"><h2>个人基本信息</h2>'
                '<label>姓名<input></label><label>电子邮箱<input></label>'+section(record(observed=True),wrapped=True,observed=True))
            empty=await service.snapshot()
            slots=await inspect_ant_resume_records(page)
            assert slots and not resolve_repeated_records(empty,profile)
            key=slots[0]['record_keys'][0]
            assert _allocation(empty,profile,'internships',slots[0]['record_keys'],key) is profile.internships[0]
            async def no_model(snapshot):
                raise AssertionError('known source attributes must not call model')
            prepared=await prepare_application(service,'anonymous-records',
                ApplicationAssistRequest(resume_id='anonymous-source',use_model=False),profile,
                guard=lambda:None,model_plan=no_model,stamp_plan=lambda plan:plan)
            assert prepared.status!='ready_for_review',(prepared.status,prepared.message)
            bound=await service.snapshot()
            for attribute in ('organization','role','description'):
                field=next(f for f in bound.fields if f.semantic_key=='experience.'+attribute)
                assert field.current_value==getattr(profile.internships[0],attribute),(prepared.status,prepared.message)
            assert prepared.record_coverage[1].matched_records==1
            assert prepared.record_coverage[1].source_total==2
            assert not prepared.record_coverage[1].can_expand
            assert await page.evaluate('window.deletes||0')==0

            # A later unrelated picker interruption may block the whole form,
            # but it cannot prevent this independently verified text batch.
            # No real navigation/network/account is used by this interruption.
            original_execute=service.execute
            async def later_widget_stops(sid,payload,**kwargs):
                if any(action.label=='国家/地区' for action in payload.actions):
                    current=await service.snapshot()
                    for attribute in ('organization','role','description'):
                        field=next(f for f in current.fields if f.semantic_key=='experience.'+attribute)
                        assert field.current_value==getattr(profile.internships[0],attribute)
                    raise ExecutionTargetChanged('synthetic unrelated widget navigation',
                        current_label='国家/地区',current_phase='target_check')
                return await original_execute(sid,payload,**kwargs)
            service.execute=later_widget_stops
            await page.set_content('<meta charset="utf-8"><h2>个人基本信息</h2>'
                '<label>姓名<input></label><label>电子邮箱<input></label>'
                '<label>国家/地区<select><option>请选择</option><option>中国</option></select></label>'+
                section(record(observed=True),wrapped=True,observed=True))
            prepared=await prepare_application(service,'anonymous-records',
                ApplicationAssistRequest(resume_id='anonymous-source',use_model=False),
                profile.model_copy(update={'country_region':'中国'}),
                guard=lambda:None,model_plan=no_model,stamp_plan=lambda plan:plan)
            assert prepared.status=='blocked',prepared
            assert any(e.kind=='fill' and e.completed==2 for e in prepared.events)
            assert sum(e.completed for e in prepared.events)==3  # identity + role/content
            assert prepared.record_coverage[1].matched_records==1
            assert await page.evaluate('window.deletes||0')==0
            service.execute=original_execute

            # The independent record batch must honour an exact user deferral,
            # not treat having a source value as permission to ignore it.
            await page.set_content('<meta charset="utf-8"><h2>个人基本信息</h2>'
                '<label>姓名<input></label><label>电子邮箱<input></label>'+
                section(record(observed=True),wrapped=True,observed=True))
            before=await service.snapshot()
            role=next(f for f in before.fields if f.semantic_key=='experience.role')
            prepared=await prepare_application(service,'anonymous-records',
                ApplicationAssistRequest(resume_id='anonymous-source',use_model=False,deferred_fields=[role]),
                profile,guard=lambda:None,model_plan=no_model,stamp_plan=lambda plan:plan)
            current=await service.snapshot()
            assert next(f for f in current.fields if f.semantic_key=='experience.role').current_value==''
            assert next(f for f in current.fields if f.semantic_key=='experience.description').current_value==profile.internships[0].description
            assert prepared.status!='ready_for_review'
            await page.set_content('<meta charset="utf-8"><h2>个人基本信息</h2>'
                '<label>姓名<input></label><label>电子邮箱<input></label>'+
                section(record(observed=True),wrapped=True,observed=True))
            before=await service.snapshot()
            anchor=next(f for f in before.fields if f.semantic_key=='experience.organization')
            prepared=await prepare_application(service,'anonymous-records',
                ApplicationAssistRequest(resume_id='anonymous-source',use_model=False,deferred_fields=[anchor]),
                profile,guard=lambda:None,model_plan=no_model,stamp_plan=lambda plan:plan)
            current=await service.snapshot()
            assert all(not f.current_value for f in current.fields if f.semantic_key.startswith('experience.'))
            assert not any(e.kind=='allocate' for e in prepared.events)
            assert prepared.status!='ready_for_review'
            # Independent projects are recognised only when the actual
            # template supplies a separately titled, anchored project record.
            project='<div class="resume-form-wrap">'+row('项目名称')+row('项目角色')+row('项目内容')+'</div>'
            await page.set_content('<meta charset="utf-8">'+section(project,title='项目经历'))
            projects=await inspect_ant_resume_records(page)
            assert len(projects)==1 and projects[0]['semantic_section']=='project'
            # Unknown sections, mixed anchors and a missing role are NOT
            # granted record allocation authority merely by sharing classes.
            for html,reason in ((section(record(),title='正式工作经历'),'section_heading_unverified'),
                         (section(record(duplicate=True)),'duplicate_question'),
                         (section(record()[:-6]+row('是否最高学历',observed=True)+'</div>'),'unknown_question'),
                         (section(record().replace('实习岗位','隐私题干不进入诊断')),'unknown_question')):
                await page.set_content('<meta charset="utf-8">'+html)
                assert not await inspect_ant_resume_records(page)
                diagnostic=await inspect_ant_resume_rejections(page)
                assert diagnostic['records'][0]['reason']==reason,diagnostic
                assert not diagnostic['records'][0]['verified']
                assert '隐私题干' not in str(diagnostic)
            # The one observed transparent layer is not permission to climb
            # arbitrary wrappers, a mixed section, or a nested record tree.
            for html in (
                section('<div>'+record()+'</div>',wrapped=True),
                section(record()+'<label>另一条问题<input></label>',wrapped=True),
                section(record(),wrapped=True).replace('<div><div class="resume-form-wrap">',
                    '<div class="unknown-template"><div class="resume-form-wrap">'),
                section(record(observed=True),wrapped=True,observed=True).replace('>删除</div>','>另一栏目</div>'),
            ):
                await page.set_content('<meta charset="utf-8">'+html)
                assert not await inspect_ant_resume_records(page)
        finally:
            await browser.close()
    print('ant_resume_records_test: OK (five-field ownership, required evidence, grounded three-field preparation, no mixing/network/add/submit)')


async def run():
    with patch('app.form_agent._knowledge_snapshot', side_effect=lambda snapshot: snapshot), \
         patch('app.form_agent.configured_model', side_effect=AssertionError('offline test must not call model')):
        await _run()


if __name__=='__main__':
    asyncio.run(run())
