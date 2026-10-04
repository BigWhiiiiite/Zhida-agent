"""Offline product integration: scoped Phoenix record expansion and deferral.

All browser requests are intercepted; no applicant data, dotenv, DB or model.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import async_playwright

from app.application_assist import prepare_application
from app.browser_models import ApplicationAssistRequest, ExecutePlanRequest
from app.browser_service import BrowserDemoService
from app.models import CandidateProfile, Education, Experience, Project
from app.phoenix_sections import inspect_phoenix_sections, expand_phoenix_section, supported


URL = 'https://fixture.zhiye.com/form?fromPage=job&jobAdId=synthetic'
HTML = r'''<!doctype html><html><head><meta charset="utf-8"><title>匿名招聘测试</title>
<style>.form-item{padding:4px}.phoenix-text-button{padding:8px;cursor:pointer}input,textarea{width:260px}</style>
</head><body><header>+86 100****0000</header>
<h1>你正在投递职位: 匿名智能体开发工程师（2027校招）(T10000)</h1>
<p>最多可投递 2 个校园招聘职位，还可投递 2 个</p>
<main id="root"></main><input type="checkbox" id="agreechk"><label for="agreechk">我承诺所填简历真实可信</label>
<button id="final">提交简历</button>
<script>
window.audit={adds:[],inputs:[],submit:0,consent:0};
const configs={
 '个人信息':[['姓名','name',true],['证件号码','id',true],['毕业学校','highest',false]],
 '教育经历':[['学校名称','school',true],['专业名称','major',true]],
 '实习经历':[['单位名称','organization',true],['职位名称','role',true],['实习内容','description',true]],
 '项目经历':[['项目名称','name',true],['职务','role',true],['项目描述','description',true],['项目中职责','responsibilities',true]],
 '获奖情况':[['获奖时间','award_date',false],['获奖项','award',false]]
};
const addRecord=(owner,values={})=>{
 const record=document.createElement('div');record.className='ux-standard-form';
 record.innerHTML='<div class="form">'+configs[owner.dataset.kind].map(([label,key,req])=>
  '<div class="form-item form-item--phoenix"><div class="form-item__title">'+
  (req?'<a class="form-item__required"><i></i></a>':'')+'<label class="form-item__text">'+label+
  '</label></div><div class="form-item__control"><input data-key="'+key+'"></div></div>').join('')+'</div>';
 Object.entries(values).forEach(([key,value])=>record.querySelector('[data-key="'+key+'"]').value=value);
 owner.querySelector('.records').appendChild(record);
};
for(const kind of Object.keys(configs)){
 const owner=document.createElement('section');owner.dataset.kind=kind;
 owner.innerHTML='<div>'+kind+'</div><div><div><div><div class="records"></div><div class="adder"></div></div></div></div></div>';
 document.querySelector('#root').appendChild(owner);
 const initial=kind==='个人信息'?{highest:'合成硕士测试校'}:kind==='教育经历'?{school:'合成本科测试校'}:
  kind==='实习经历'?{organization:'合成企业乙'}:kind==='项目经历'?{name:'合成项目丙'}:{};
 addRecord(owner,initial);
 if(['教育经历','实习经历','项目经历'].includes(kind)){
  // Observed live shape: a plain own sibling with an icon and add caption.
  // The website's phoenix-text-button class belongs to UPLOAD, not add.
  owner.querySelector('.adder').innerHTML='<div class="anonymous-add"><svg width="10" height="10"></svg><span>添加'+kind+'</span></div>';
  owner.querySelector('.anonymous-add').addEventListener('click',()=>{
   audit.adds.push(kind);if(window.noop)return;addRecord(owner);if(window.doubleAdd)addRecord(owner);
  });
 }
}
document.addEventListener('input',e=>audit.inputs.push({kind:e.target.closest('section')?.dataset.kind,key:e.target.dataset.key,value:e.target.value}));
document.querySelector('#final').onclick=()=>audit.submit++;
document.querySelector('#agreechk').onclick=()=>audit.consent++;
</script></body></html>'''


async def run():
    profile = CandidateProfile(name='合成测试人', education=[
        Education(school='合成硕士测试校', degree='硕士', major='硕士自己的专业'),
        Education(school='合成本科测试校', degree='本科', major='本科自己的专业')], internships=[
        Experience(organization='合成企业甲', role='甲实习职位', description='甲的工作'),
        Experience(organization='合成企业乙', role='乙实习职位', description='乙的工作')], projects=[
        Project(name='合成项目甲', role='个人项目', description='搭建甲项目并验证甲数据。'),
        Project(name='合成项目乙', role='核心开发', description='乙的系统。负责乙的联调。'),
        Project(name='合成项目丙', role='丙角色', description='丙的系统。', responsibilities='丙的职责')])
    request = ApplicationAssistRequest(resume_id='synthetic', use_model=False, defer_government_id=True)

    async def no_model(_):
        raise AssertionError('confirmed synthetic facts do not need a model')

    assert supported(URL) and not supported('https://evil.test/form')
    assert not supported('https://fixture.zhiye.com.evil.test/form')
    assert not supported('https://fixture.zhiye.com/campus/detail')
    with patch('dotenv.load_dotenv', side_effect=AssertionError('dotenv forbidden')), \
         patch('sqlite3.connect', side_effect=AssertionError('DB forbidden')), \
         patch('app.application_knowledge.retrieve_knowledge', return_value={}):
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(channel='chrome', headless=True)
            try:
                context = await browser.new_context(service_workers='block')
                await context.route('**/*', lambda route: route.fulfill(body=HTML, content_type='text/html'))
                page = await context.new_page()
                await page.goto(URL)
                service = BrowserDemoService()
                service.page,service.context,service.session_id = page,context,'fixture'
                inventory = await service.record_inventory('fixture')
                assert {r['kind'] for r in inventory} == {'education','internships','projects'}, inventory
                assert all(r['record_count']==1 and r['selector'] for r in inventory), inventory
                old_project = next(r for r in inventory if r['kind']=='projects')
                result = await prepare_application(service,'fixture',request,profile,guard=lambda:None,
                    model_plan=no_model,stamp_plan=lambda p:p)
                assert result.status=='needs_user', result.model_dump()
                assert all(not r.missing_names and not r.ambiguous and r.matched_records==r.source_total
                    for r in result.record_coverage), result.model_dump()
                assert {r.kind:r.matched_records for r in result.record_coverage}=={'education':2,'internships':2,'projects':3}
                assert [f.label for f in result.pre_submit.required_missing if '承诺' not in f.label]==['证件号码']
                assert not result.pre_submit.ready
                audit = await page.evaluate('audit')
                assert audit['adds']==['教育经历','实习经历','项目经历','项目经历'], audit
                assert audit['submit']==audit['consent']==0
                assert not any(r['key']=='id' for r in audit['inputs'])
                assert await page.locator('[data-key="id"]').input_value()==''
                for kind, sources, anchor in [('教育经历',profile.education,'school'),
                    ('实习经历',profile.internships,'organization'),('项目经历',profile.projects,'name')]:
                    rows=page.locator('section[data-kind="'+kind+'"] .ux-standard-form')
                    for i in range(await rows.count()):
                        name=await rows.nth(i).locator('[data-key="'+anchor+'"]').input_value()
                        source=next(r for r in sources if getattr(r,anchor)==name)
                        for key in (['major'] if kind=='教育经历' else ['role']):
                            assert await rows.nth(i).locator('[data-key="'+key+'"]').input_value()==getattr(source,key)
                # A second one-click run must not duplicate or rewrite records.
                again=await prepare_application(service,'fixture',request,profile,guard=lambda:None,
                    model_plan=no_model,stamp_plan=lambda p:p)
                assert again.status=='needs_user' and again.rounds==0, again.model_dump()
                assert await page.evaluate('audit')==audit
                try:
                    await service.expand_missing_section('fixture',old_project)
                except ValueError:
                    pass
                else:
                    raise AssertionError('stale record inventory must not be clicked')
                assert await page.evaluate('audit')==audit
                # Upload uses original file name, not internal storage UUID;
                # native selection is not claimed as server receipt/submission.
                await page.evaluate("document.querySelector('main').insertAdjacentHTML('beforeend','<div><label for=resume>简历附件</label><input type=file id=resume></div>')")
                with patch.object(Path,'read_bytes',return_value=b'anonymous synthetic PDF'):
                    uploaded=await service.execute('fixture',ExecutePlanRequest(actions=[]),Path('stored-fixture.pdf'),
                        resume_filename='anonymous-original.pdf')
                assert uploaded.verified==1 and uploaded.failed==0, uploaded.model_dump()
                assert await page.locator('#resume').evaluate('el=>el.files[0].name')=='anonymous-original.pdf'
                assert '尚不代表官网接收' in uploaded.results[0].message
                assert not await page.locator('#agreechk').is_checked()
                # Empty initial slots are explicitly allocated, not silently
                # inferred by ordinal, and highest-degree summary is not a record.
                await page.goto(URL)
                await page.locator('section[data-kind="教育经历"] [data-key="school"]').fill('')
                empty=await prepare_application(service,'fixture',request,profile,guard=lambda:None,
                    model_plan=no_model,stamp_plan=lambda p:p)
                assert any(e.kind=='allocate' for e in empty.events), empty.model_dump()
                assert all(not r.missing_names and not r.ambiguous for r in empty.record_coverage)
                # No-op and double-add controls are clicked once and then stop.
                for flag in ['noop','doubleAdd']:
                    await page.goto(URL)
                    await page.evaluate('window.'+flag+'=true')
                    row=next(r for r in await inspect_phoenix_sections(page) if r['semantic_section']=='project')
                    try:
                        await expand_phoenix_section(page,row['id'])
                    except ValueError:
                        pass
                    else:
                        raise AssertionError(flag+' must fail exact +1 verification')
                    assert await page.evaluate('audit.adds')==['项目经历']
                # Duplicate controls/section ownership stay fail-closed.
                await page.goto(URL)
                await page.evaluate("const a=document.querySelector('[data-kind=项目经历] .adder');a.appendChild(a.firstElementChild.cloneNode(true))")
                row=next(r for r in await inspect_phoenix_sections(page) if r['semantic_section']=='project')
                assert row['selector']==''
                await page.evaluate("const s=document.querySelector('[data-kind=教育经历]');s.parentElement.appendChild(s.cloneNode(true))")
                assert not any(r['semantic_section']=='education' for r in await inspect_phoenix_sections(page))
            finally:
                await browser.close()
    print('phoenix_sections_test: OK (2 education/2 internships/3 projects, no duplicate/ID/declaration/submit, exact scoped expansion)')


if __name__=='__main__':
    asyncio.run(run())
