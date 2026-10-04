"""Real DOM integration for the product assist path, entirely synthetic/offline.

Uses a fresh ephemeral headless browser with every request intercepted. No
existing browser context, applicant profile, credentials, database or model is
used. Only the knowledge-storage boundary is stubbed; observation, expansion,
identity allocation, rule mapping, execution and read-back are production code.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from playwright.async_api import async_playwright

from app.application_assist import prepare_application
from app.browser_models import ApplicationAssistRequest
from app.browser_service import BrowserDemoService
from app.models import CandidateProfile, Education, Project


URL = "https://talent.autohome.com.cn/recruit-delivery.html?pid=fixture-only"
SESSION = "offline-product-browser"

HTML = r'''<!doctype html><html><head><meta charset="utf-8"><title>匿名申请表测试</title>
<style>.icard{margin:8px;padding:8px}.row{display:flex;gap:20px}.col{min-width:250px}
.labeltag{display:flex}.add,.addbtn,.save{cursor:pointer;padding:8px}textarea{width:450px}</style>
</head><body><h1>填写简历：合成工程师岗位</h1><form class="validform">
<div class="icard"><div class="title">基本信息</div><div class="content"><div class="row">
 <div class="col"><div class="labeltag"><div class="requireTag">*</div><div class="label">姓名</div></div>
  <div class="control"><input id="candidate-name" data-bind="value:resumeDetail().Name" datatype="*"></div></div>
 <div class="col"><div class="labeltag"><div class="requireTag">*</div><div class="label">邮箱</div></div>
  <div class="control"><input id="candidate-email" data-bind="value:resumeDetail().Email" datatype="*"></div></div>
</div></div></div>
<div class="icard"><div class="title">教育经历</div><div class="content">
 <div class="contentFirstRow"><div class="tip">请从本科学历开始填写</div></div>
 <div data-bind="template:{name:'eduTemplate',foreach:education}">
  <div class="numtxt">教育经历-1</div><div class="row"><div class="col">
   <div class="labeltag"><div class="requireTag">*</div><div class="label">学校名称</div></div>
   <div class="control"><input id="school" data-bind="value: School" value="合成本科测试院校" datatype="*"></div>
  </div></div><div class="option"><div class="add" data-bind="click: $parent.addEdu">增加教育经历</div></div>
 </div>
</div></div>
<div class="icard" id="project-card"><div class="title">项目经历</div><div class="content">
 <div class="contentFirstRow"><div class="addbtn" data-bind="click: addProject">增加项目经历</div></div>
 <div id="project-root" data-bind="template:{name:'projectTemplate',foreach:projects}"></div>
</div></div></form>
<div class="scard fixbottom"><div><input type="checkbox" class="chk" id="agreechk">
 <div class="txt">我承诺所填简历真实可信，并承担相应的法律责任。</div></div>
 <div class="save">提交简历</div><div class="cancel">取消</div></div>
<script>
 window.testAudit={adds:0,submit:0,consent:0,delete:0,inputs:[]};
 const root=document.querySelector('#project-root');
 const columns=[['ProjectName','项目名称'],['Title','项目角色'],['StartDate','开始时间'],
                ['EndDate','结束时间'],['ProjectDescription','项目描述']];
 const addRecord=(initialName='')=>{
   const number=root.querySelectorAll('.option').length+1;
   const fields=columns.map(([binding,label])=>{
     const attrs=`id="project-${number}-${binding}" data-bind="value: ${binding}" datatype="*"`;
     const control=binding==='ProjectDescription'?`<textarea ${attrs}></textarea>`:`<input ${attrs}>`;
     return `<div class="col"><div class="labeltag"><div class="requireTag">*</div><div class="label">${label}</div></div>
       <div class="control">${control}</div></div>`;
   });
   root.insertAdjacentHTML('beforeend',`<div class="numtxt">项目经历-${number}</div>
    <div class="row">${fields.slice(0,2).join('')}</div><div class="row">${fields.slice(2,4).join('')}</div>
    <div class="row">${fields[4]}</div><div class="option"><div class="del">删除项目经历</div>
    <div class="add" data-bind="click: $parent.addProject">增加项目经历</div></div><div class="splitline"></div>`);
   root.querySelector(`#project-${number}-ProjectName`).value=initialName;
   document.querySelector('#project-card .addbtn').style.display='none';
 };
 // Existing record is the LAST profile item, intentionally not the first.
 // Its identity must be reserved before allocating the two new empty groups.
 addRecord('合成项目丙：已存在记录');
 document.querySelector('#project-card').addEventListener('click',event=>{
   if(event.target.matches('.add,.addbtn')){testAudit.adds++;addRecord();}
   if(event.target.matches('.del'))testAudit.delete++;
 });
 document.querySelector('.save').addEventListener('click',()=>testAudit.submit++);
 document.querySelector('#agreechk').addEventListener('click',()=>testAudit.consent++);
 document.addEventListener('input',event=>testAudit.inputs.push({id:event.target.id,value:event.target.value}));
</script></body></html>'''


async def run():
    profile = CandidateProfile(name="合成测试人", email="synthetic@example.test",
        education=[Education(school="合成本科测试院校", degree="本科")], projects=[
            Project(name="合成项目甲：知识库工具", role="甲角色",
                    start_date="2023-01-11", end_date="2023-06-21", description="只属于甲的检索描述。"),
            Project(name="合成项目乙：表单验证工具", role="乙角色",
                    start_date="2024-02-12", end_date="2024-07-22", description="只属于乙的验证描述。"),
            Project(name="合成项目丙：已存在记录", role="丙角色",
                    start_date="2025-03-13", end_date="2025-08-23", description="只属于丙的已有项目描述。"),
        ])
    expected = [profile.projects[2], profile.projects[0], profile.projects[1]]
    request = ApplicationAssistRequest(resume_id="synthetic-resume", use_model=False)
    guards = []

    async def no_model(_snapshot):
        raise AssertionError("known synthetic facts must not invoke a model")

    def guard():
        guards.append(True)

    with patch("dotenv.load_dotenv", side_effect=AssertionError("dotenv forbidden")), \
         patch("sqlite3.connect", side_effect=AssertionError("database forbidden")), \
         patch("app.application_knowledge.retrieve_knowledge", return_value={}):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            try:
                context = await browser.new_context(service_workers="block")
                intercepted = []

                async def intercept(route):
                    intercepted.append(route.request.url)
                    await route.fulfill(body=HTML, content_type="text/html")

                await context.route("**/*", intercept)
                page = await context.new_page()
                await page.goto(URL)
                service = BrowserDemoService()
                service.page, service.context, service.session_id = page, context, SESSION
                before = await service.expandable_sections(SESSION)
                project = next(item for item in before if item["kind"] == "projects")
                assert project["record_count"] == 1
                result = await prepare_application(service, SESSION, request, profile, guard=guard,
                                                  model_plan=no_model, stamp_plan=lambda plan: plan)
                assert result.status == "ready_for_review", result.model_dump()
                assert result.rounds >= 1 and any(event.kind == "fill" and event.completed for event in result.events)
                assert len([event for event in result.events if event.kind == "expand"]) == 2
                assert not any(event.failed for event in result.events)
                for index, record in enumerate(expected, 1):
                    for binding, attribute in (("ProjectName", "name"), ("Title", "role"),
                                               ("StartDate", "start_date"), ("EndDate", "end_date"),
                                               ("ProjectDescription", "description")):
                        actual = await page.locator(f"#project-{index}-{binding}").input_value()
                        assert actual == getattr(record, attribute), (index, binding, actual)
                assert await page.locator("#candidate-name").input_value() == profile.name
                assert await page.locator("#candidate-email").input_value() == profile.email
                assert not await page.locator("#agreechk").is_checked()
                assert len(result.pre_submit.required_missing) == 1
                assert "承诺" in result.pre_submit.required_missing[0].label
                assert not result.pre_submit.ready  # User declaration is deliberately outstanding.
                assert result.review.summary.matched == 18
                audit = await page.evaluate("testAudit")
                assert audit["adds"] == 2
                assert audit["submit"] == audit["consent"] == audit["delete"] == 0
                # The first actions on each NEW group must seed its own identity.
                for index, record in enumerate(expected[1:], 2):
                    writes = [item for item in audit["inputs"] if item["id"].startswith(f"project-{index}-")]
                    assert writes[0] == {"id": f"project-{index}-ProjectName", "value": record.name}
                inventory = await service.expandable_sections(SESSION)
                project = next(item for item in inventory if item["kind"] == "projects")
                observed_keys = {field.container_key for field in result.snapshot.fields
                                 if field.semantic_key.startswith("project.")}
                assert set(project["record_keys"]) == observed_keys and len(observed_keys) == 3

                # Reusing the product button must not add records, rewrite correct
                # fields, reupload, accept declarations, or invoke final submission.
                again = await prepare_application(service, SESSION, request, profile, guard=guard,
                                                 model_plan=no_model, stamp_plan=lambda plan: plan)
                assert again.status == "ready_for_review" and again.rounds == 0, again.model_dump()
                assert await page.evaluate("testAudit") == audit
                assert not await page.locator("#agreechk").is_checked()
                assert len(intercepted) >= 1 and guards
            finally:
                await browser.close()
    print("application_assist_browser_test: OK (real DOM, 2 new project anchors, reversed existing identity, 18 matched, no repeat/consent/submit)")


if __name__ == "__main__":
    asyncio.run(run())
