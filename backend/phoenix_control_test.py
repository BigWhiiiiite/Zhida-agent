"""Anonymous intercepted regressions for Phoenix question/record ownership."""
from __future__ import annotations

import asyncio
from playwright.async_api import async_playwright

from app.browser_models import ExecutePlanRequest, FillAction
from app.browser_service import BrowserDemoService


def question(label, control, required=False):
    flag = '<a class="form-item__required"><i class="icon-cus-bitian"></i></a>' if required else ''
    return '<div class="form-item form-item--phoenix"><div class="form-item__title">' + flag + \
        '<label class="form-item__text">' + label + '</label></div><div class="form-item__control">' + control + '</div></div>'


def record(section, fields):
    content = '<div class="ux-standard-form"><div class="form">' + fields + '</div></div>'
    for _ in range(4):
        content = '<div class="Shell">' + content + '</div>'
    return '<section><div>' + section + '</div>' + content + '</section>'


RADIOS = '<div class="phoenix-radio-group">' + ''.join(
    '<div class="phoenix-radio" onclick="this.parentElement.querySelectorAll(\'.phoenix-radio__dot\')'
    '.forEach(n=>n.style.opacity=0);this.querySelector(\'.phoenix-radio__dot\').style.opacity=1">'
    '<div class="phoenix-radio__wrapper"><div class="phoenix-radio__dot" style="opacity:0;width:8px;height:8px"></div>'
    '<span class="phoenix-radio__radio-text">' + option + '</span></div></div>' for option in ['男', '女', '保密']) + '</div>'
SELECT = '''<div class="phoenix-select" onclick="document.getElementById('menu').hidden=false">
<span class="phoenix-select__calcEle"></span><div class="phoenix-select__placeHolder">请选择</div>
<ul class="phoenix-select__content"><li class="phoenix-select__inputWrapper"><input class="phoenix-select__input"></li></ul></div>'''
HTML = '<header class="print-nav"><span>+86 100****0000</span><input placeholder="搜索职位关键词"></header>' + \
    '<main><div><h1>你正在投递职位: 匿名智能体开发工程师（2027校招）(T10000)</h1>' + \
    '<p>最多可投递 2 个校园招聘职位，还可投递 2 个</p></div>' + \
    record('个人信息', question('姓名', '<input>', True) + question('性别', RADIOS, True) + question('现居住地', SELECT, True)) + \
    record('教育经历', question('学校名称', '<input value="匿名本科院校">', True) + question('专业名称', '<input>')) + \
    record('教育经历', question('学校名称', '<input value="匿名硕士院校">', True) + question('专业名称', '<input>')) + \
    record('实习经历', '<div class="form-part">' + question('单位名称', '<input value="匿名组织">', True) +
           '</div><div class="form-part">' + question('实习内容', '<textarea></textarea>') + '</div>') + \
    '<ul class="phoenix-selectList__list" id="menu" hidden>' + ''.join(
        '<li class="phoenix-selectList__listItem" onclick="document.querySelector(\'.phoenix-select__content\')'
        '.insertAdjacentHTML(\'afterbegin\',\'<li>北京</li>\');this.parentElement.hidden=true">' + city + '</li>'
        for city in ['北京', '上海']) + '</ul>' + \
    '<button type="submit" onclick="document.body.dataset.final=\'yes\'">提交申请</button></main>' + \
    '''<script>document.addEventListener('click',event=>{if(!event.target.closest('.phoenix-select,#menu'))document.getElementById('menu').hidden=true;});</script>'''


async def run():
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        try:
            context = await browser.new_context(service_workers='block')
            requests = []

            async def serve(route):
                requests.append(route.request.url)
                await route.fulfill(body=HTML, content_type='text/html; charset=utf-8')

            await context.route('**/*', serve)
            await context.route_web_socket('**/*', lambda socket: socket.close())
            page = await context.new_page()
            await page.goto('https://fixture.zhiye.com/form?jobAdId=anonymous')
            service = BrowserDemoService()
            service.page, service.context, service.session_id = page, context, 'fixture'
            snap = await service.snapshot()
            assert not any('搜索' in f.label for f in snap.fields)
            gender = [f for f in snap.fields if f.label == '性别']
            assert len(gender) == 3 and {f.option_label for f in gender} == {'男', '女', '保密'}
            assert len({f.control_group_key for f in gender}) == 1
            assert all(f.field_type == 'radio' and f.required and f.current_value == 'false' for f in gender)
            city = next(f for f in snap.fields if f.label == '现居住地')
            assert city.field_type == 'combobox' and city.options == ['北京', '上海'] and city.required
            search = page.locator('.phoenix-select__input')
            await search.fill('北京')
            assert await service._read_field_value(city) == '', 'Search text cannot prove selected value'
            await search.fill('')
            schools = [f for f in snap.fields if f.label == '学校名称']
            assert len(schools) == 2 and len({f.container_key for f in schools}) == 2
            assert all(f.section == '教育经历' and f.entity_scope == 'education:unspecified' for f in schools)
            internship = [f for f in snap.fields if f.section == '实习经历']
            assert len(internship) == 2 and len({f.container_key for f in internship}) == 1
            assert {f.semantic_key for f in internship} == {'experience.organization', 'experience.description'}
            male = next(f for f in gender if f.option_label == '男')
            result = await service.execute('fixture', ExecutePlanRequest(actions=[
                FillAction(selector=male.selector, label='性别', action='check', value=True, user_confirmed=True, confidence=1),
                FillAction(selector=city.selector, label='现居住地', action='select', value='北京', user_confirmed=True, confidence=1),
            ]))
            assert result.verified == 2 and result.failed == 0, result.model_dump()
            final = await service.snapshot()
            gender = [f for f in final.fields if f.label == '性别']
            assert sum(f.current_value == 'true' for f in gender) == 1
            assert next(f for f in gender if f.current_value == 'true').option_label == '男'
            check = await service.pre_submit_check('fixture')
            assert check.required_total == 6 and not check.ready, check.model_dump()
            assert [f.label for f in check.required_missing] == ['姓名']
            workflow = await service.workflow_state('fixture')
            assert workflow.authenticated and workflow.job_title.endswith('(T10000)'), workflow.model_dump()
            assert await page.locator('body').get_attribute('data-final') is None
            # The observed Beisen area chooser has no ARIA option/listbox.
            # It is still a scoped menu, not a free text input.
            await page.set_content(HTML.replace('class="phoenix-selectList__list"', 'class="area-data-container"')
                                   .replace('class="phoenix-selectList__listItem"', 'class="area-item-name"'))
            area = next(f for f in (await service.snapshot()).fields if f.label == '现居住地')
            assert area.options == ['北京', '上海'], area.model_dump()
            assert await service._select_custom(area, ['北京']) == ['北京']
            assert await service._read_field_value(area) == '北京'
            # A region row has separate drilling and radio-commit affordances.
            # A text-row click must NOT be mistaken for selecting the city.
            area_html = HTML.replace('class="phoenix-selectList__list"', 'class="area-data-container"')
            area_html = area_html.replace('class="phoenix-selectList__listItem"', 'class="area-item-name"')
            # Build the observed icon shape while keeping all data anonymous.
            begin=area_html.index('<ul class="area-data-container"')
            end=area_html.index('</ul>',begin)+len('</ul>')
            menu='''<ul class="area-data-container" id="menu" hidden><li class="area-item-name" onclick="document.body.dataset.drill='yes'">
              <span class="icon-container visible" onclick="event.stopPropagation();document.querySelector('.phoenix-select__content').insertAdjacentHTML('afterbegin','<li>北京</li>');this.closest('#menu').hidden=true"><svg class="area-icon-RadioUnchecked" width="20" height="20"></svg></span><span class="area-text-label">北京</span>
              </li><li class="area-item-name">上海</li></ul>'''
            area_html=area_html[:begin]+menu+area_html[end:]
            await page.set_content(area_html)
            area = next(f for f in (await service.snapshot()).fields if f.label == '现居住地')
            assert await service._select_custom(area, ['北京']) == ['北京']
            assert await service._read_field_value(area) == '北京'
            assert await page.locator('body').get_attribute('data-drill') is None
            await page.set_content(record('个人信息', question('是否可提前实习',
                RADIOS.replace('>男<','>是<').replace('>女<','>否<').replace('>保密<','>暂不确定<'), True)))
            yes_no=(await service.snapshot()).fields
            assert len(yes_no)==3 and all(f.label=='是否可提前实习' for f in yes_no)
            assert len({f.control_group_key for f in yes_no})==1
            assert all(f.options==['是','否','暂不确定'] for f in yes_no)
            # The real widget animates after clicking; do not permanently mark
            # an immediate false read as failed once settled state is verified.
            delayed=RADIOS.replace("this.querySelector('.phoenix-radio__dot').style.opacity=1",
                "setTimeout(()=>this.querySelector('.phoenix-radio__dot').style.opacity=1,400)")
            await page.set_content(record('个人信息',question('性别',delayed,True)))
            male=next(f for f in (await service.snapshot()).fields if f.option_label=='男')
            result=await service.execute('fixture',ExecutePlanRequest(actions=[FillAction(
                selector=male.selector,label='性别',action='check',value=True,user_confirmed=True,confidence=1)]))
            assert result.verified==1 and result.failed==0, result.model_dump()
            # Actual Phoenix 至今 is a sibling of the end-date question.
            # Own record/semantic identity must survive that layout, without
            # inheriting the end-date required mark or another record's source.
            from app.form_agent import create_local_form_plan
            from app.models import CandidateProfile, Experience, Project
            checkbox = '<div class="phoenix-checkbox"><span class="phoenix-checkbox__box"><input type="checkbox" class="phoenix-checkbox__input"></span><span class="phoenix-checkbox__text">至今</span></div>'
            duration = '<div class="fields-col"><div class="end-date-owner">' + question('结束时间','<input>',True) + '<div>' + checkbox + '</div></div></div>'
            await page.set_content(record('实习经历', question('单位名称','<input value="匿名组织">') + duration) +
                                   record('项目经历', question('项目名称','<input value="匿名项目">') + duration))
            ongoing=(await service.snapshot())
            checks=[f for f in ongoing.fields if f.field_type=='checkbox']
            assert len(checks)==2 and {f.semantic_key for f in checks}=={'experience.current','project.current'}
            assert all(not f.required and f.current_value=='false' for f in checks)
            for f in checks:
                assert any(g.container_key==f.container_key and g.semantic_key in {'experience.organization','project.name'} for g in ongoing.fields)
            profile=CandidateProfile(internships=[Experience(organization='匿名组织',current=True,end_date='至今'),Experience(organization='另一组织',end_date='2024-10')],
                                     projects=[Project(name='匿名项目',end_date='至今'),Project(name='另一项目',end_date='2023-12')])
            plan=create_local_form_plan(ongoing,profile)
            actions=[a for a in plan.actions if a.selector in {f.selector for f in checks}]
            assert len(actions)==2 and all(a.action=='check' and a.value is True for a in actions), [a.model_dump() for a in actions]
            result=await service.execute('fixture',ExecutePlanRequest(actions=actions))
            assert result.verified==2 and result.failed==0, result.model_dump()
            assert all(f.current_value=='true' for f in (await service.snapshot()).fields if f.field_type=='checkbox')
            # Ambiguous owner and unowned "至今" stay conservative.
            await page.set_content(record('项目经历',question('项目名称','<input value="匿名项目">')+
                '<div>'+question('结束时间','<input>')+question('开始时间','<input>')+checkbox+'</div>')+checkbox)
            unowned=[f for f in (await service.snapshot()).fields if f.field_type=='checkbox']
            assert all(not f.semantic_key.endswith('.current') for f in unowned)
            # Self-assessment belongs to the committed language in this record.
            # Identical proficiency labels in English/Japanese are not aliases.
            def language_record(language):
                selected='<div class="phoenix-select"><ul class="phoenix-select__content"><li>'+language+'</li></ul></div>'
                return record('语言能力',question('语言类型',selected)+question('掌握程度',SELECT))
            await page.set_content(language_record('英语')+language_record('日语')+language_record(''))
            languages=(await service.snapshot()).fields
            assessments=[f for f in languages if f.semantic_key=='language.proficiency']
            assert len(assessments)==3 and {f.entity_scope for f in assessments}=={'language:英语','language:日语','language:unspecified'}
            from app.form_agent import _saved_answer_match, _local_safe_plan
            from app.models import ApplicationAnswerMemory
            from datetime import datetime,timezone
            english=next(f for f in assessments if f.entity_scope=='language:英语')
            profile=CandidateProfile(languages=['IELTS：6.5'],application_answer_memory=[ApplicationAnswerMemory(
                id='english',question='掌握程度',normalized_question='掌握程度',semantic_key='language.proficiency',
                entity_scope='language:英语',field_type='combobox',value='精通',updated_at=datetime.now(timezone.utc))])
            assert _saved_answer_match(english,profile).value=='精通'
            assert all(not _saved_answer_match(f,profile).value for f in assessments if f is not english)
            profile.application_answer_memory=[]
            plan=_local_safe_plan(await service.snapshot(),profile)
            assert all(a.action=='ask_user' for a in plan.actions if a.selector in {f.selector for f in assessments})
            assert all(url.startswith('https://fixture.zhiye.com/') for url in requests)
        finally:
            await browser.close()
    print('phoenix_control_test: OK (radio/select readback, required groups, isolated records, no final submission)')


if __name__ == '__main__':
    asyncio.run(run())
