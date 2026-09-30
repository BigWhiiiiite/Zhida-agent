"""Offline ATS regression benchmark with synthetic, non-personal page fixtures.

Runs the production discovery, mapping, browser execution and read-back code.
All HTTP requests are intercepted; no real applications or model calls occur.
Output is JSON so before/after runs can be compared without a model judge.
"""
from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

from playwright.async_api import async_playwright

from app.application_agent import decide_application_step
from app.ats_adapters import inspect_application_page, start_application
from app.ats_registry import resolve_site_route
from app.browser_models import BrowserSnapshot, ExecutePlanRequest, FillAction, PageField
from app.browser_service import BrowserDemoService
from app.form_agent import create_local_form_plan
from app.models import CandidateProfile, Education


PROFILE = CandidateProfile(
    name="测试候选人", location="北京", target_cities=["北京"], country_region="中国",
    education=[
        Education(school="示例硕士大学", college="研究生计算机学院", degree="硕士"),
        Education(school="示例本科大学", college="本科软件学院", degree="本科"),
    ],
)
CASES = [
    ("tencent", "https://join.qq.com/post_detail.html?postid=test", "tencent-campus", "atsx"),
    ("moka", "https://app.mokahr.com/campus-recruitment/test/1#/job/test", "moka-campus", "moka"),
    ("beisen", "https://example.italent.cn/job/test", "beisen-italent", "beisen"),
    ("generic", "https://careers.example.test/job/test", "generic", "generic"),
    ("custom_domain", "https://jobs.example.test/job/test", "moka-campus", "moka"),
]
EXPECTED = {
    "candidate_name": "测试候选人", "country": "中国大陆", "work_city": "北京市",
    "master_school": "示例硕士大学", "master_college": "研究生计算机学院",
    "bachelor_school": "示例本科大学", "bachelor_college": "本科软件学院",
}


def verify_mapping_boundaries() -> int:
    cases = [
        # Nearby questions cannot redefine a field's own title.
        (PageField(selector="#college", label="学院名称", section="硕士教育经历",
                   nearby_labels=["邮箱", "期望工作城市"], context="期望工作城市 北京"),
         PROFILE, "fill", "研究生计算机学院"),
        # One visible option does not imply a fact the candidate never supplied.
        (PageField(selector="#city", label="期望工作城市", required=True,
                   field_type="select-one", options=["深圳"]), PROFILE, "ask_user", ""),
        (PageField(selector="#agree", label="是否接受调剂", required=True,
                   field_type="select-one", options=["是"]), PROFILE, "ask_user", ""),
    ]
    for field, profile, action, value in cases:
        plan = create_local_form_plan(BrowserSnapshot(session_id="mapping", url="https://example.test",
                                      title="Fixture", fields=[field]), profile)
        assert (plan.actions[0].action, plan.actions[0].value) == (action, value), plan.model_dump()
    # A wrongly prefilled master's block may not recruit a bachelor's record
    # simply because its current school/college match that bachelor's profile.
    bachelor_only = CandidateProfile(education=[PROFILE.education[1]])
    fields = [PageField(selector="#school", label="院校名称", section="硕士教育经历",
                        container_key="master", current_value=PROFILE.education[1].school),
              PageField(selector="#college", label="学院名称", section="硕士教育经历",
                        container_key="master", current_value=PROFILE.education[1].college)]
    plan = create_local_form_plan(BrowserSnapshot(session_id="mapping", url="https://example.test",
                                 title="Fixture", fields=fields), bachelor_only)
    assert all(action.action == "ask_user" for action in plan.actions)
    return len(cases) + 1


def application_html(vendor: str) -> str:
    control = {"atsx": "atsx-select-selector", "moka": "moka-select",
               "beisen": "beisen-select", "generic": "ant-select-selector"}[vendor]
    popup = "ant-select-dropdown" if vendor == "generic" else f"{vendor}-select-dropdown"
    option = "ant-select-item-option" if vendor == "generic" else f"{vendor}-select-option"
    if vendor == "atsx":
        option = "atsx-select-item-option"
    label = "atsx-form-item-label" if vendor == "atsx" else "field-label"
    return f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
    <form onsubmit="event.preventDefault();document.body.dataset.submitted='yes'">
      <h1>在线申请资料</h1>
      <label>姓名<input name="candidate_name" required></label>
      <label>国家/地区<select name="country" required><option value="">请选择</option>
        <option value="cn">中国大陆</option><option disabled>不存在的地区</option></select></label>
      <div class="{vendor}-form-item"><div class="{label}">期望工作城市</div>
        <div class="{control}" name="work_city" aria-controls="city:options" aria-required="true"
          onclick="setTimeout(()=>document.getElementById('city:options').hidden=false,250)">
          <span class="selected-value">请选择</span></div></div>
      <div id="city:options" class="{popup}" hidden>
        <div class="{option}" onclick="const c=document.querySelector('[name=work_city]');c.setAttribute('aria-valuetext',this.textContent);c.querySelector('span').textContent=this.textContent;this.parentElement.hidden=true">北京市</div>
        <div class="{option}" aria-disabled="true">上海市</div>
      </div>
      <div role="listbox" id="unrelated"><div role="option"
        onclick="document.body.dataset.wrong='yes'">上海市</div></div>
      <section><h2>教育经历</h2>
        <div class="education-item"><h3>硕士教育经历</h3>
          <label>院校名称<input name="master_school" required></label>
          <label>学院名称<input name="master_college" required></label></div>
        <div class="education-item"><h3>本科教育经历</h3>
          <label>院校名称<input name="bachelor_school" required></label>
          <label>学院名称<input name="bachelor_college" required></label></div></section>
      <fieldset><legend>除上述选择外，是否还接受其他城市分配</legend>
        <label><input type="radio" name="relocate" value="yes">是</label>
        <label><input type="radio" name="relocate" value="no">否</label></fieldset>
      <label>紧急联系人姓名<input name="emergency_name"></label>
      <button type="submit">提交申请</button>
    </form>
    <script>document.addEventListener('keydown',e=>{{if(e.key==='Escape')document.getElementById('city:options').hidden=true}})</script>
    </body></html>"""


async def run_case(browser, case) -> dict:
    name, url, adapter, vendor = case
    context = await browser.new_context()
    page = await context.new_page()

    async def respond(route):
        is_form = "/apply" in route.request.url
        content = application_html(vendor) if is_form else (
            # A loaded detail fixture must contain actual job content. A URL,
            # heading and Apply button alone may be an incomplete SPA shell.
            '<h1>测试岗位</h1><section><h2>岗位职责</h2>'
            '<p>开发智能体应用，并维护可靠的业务服务与自动化测试。</p></section>'
            '<section><h2>任职要求</h2><p>熟悉 Python 与服务端工程开发。</p></section>'
            '<a href="/apply">立即申请</a>')
        await route.fulfill(content_type="text/html; charset=utf-8", body=content)

    await context.route("**/*", respond)
    service = BrowserDemoService()
    service.page, service.context, service.session_id = page, context, name
    await page.goto(url)
    state = await inspect_application_page(page, name)
    assert state.stage == "job_detail", (name, state.stage)
    await start_application(page)
    snapshot = await service.snapshot()
    state = await service.workflow_state(name)
    assert state.site_route.adapter == adapter == snapshot.site_route.adapter
    assert state.stage == "review" and state.final_submit_present
    fields = {field.name: field for field in snapshot.fields}
    assert fields["work_city"].options == ["北京市"], fields["work_city"].model_dump()
    assert "不存在的地区" not in fields["country"].options
    radios = [field for field in snapshot.fields if field.name == "relocate"]
    assert len(radios) == 2 and all("其他城市分配" in field.question_text for field in radios)
    before = await service.pre_submit_check(name)
    decision = await decide_application_step(state, snapshot, PROFILE, before, use_model=False)
    assert decision.next_action == "analyze_and_fill", decision.model_dump()
    plan = create_local_form_plan(snapshot, PROFILE)
    by_selector = {action.selector: action for action in plan.actions}
    planned, correct = 0, 0
    for field_name, wanted in EXPECTED.items():
        action = by_selector[fields[field_name].selector]
        executable = action.action in {"fill", "select", "check"} and action.confidence >= .85
        planned += int(executable)
        correct += int(executable and action.value == wanted)
        assert executable and action.value == wanted, (name, field_name, action.model_dump())
    assert by_selector[fields["emergency_name"].selector].action == "ask_user"
    assert all(by_selector[field.selector].action == "ask_user" for field in radios)
    result = await service.execute(name, ExecutePlanRequest(actions=plan.actions))
    assert result.failed == 0, [r.model_dump() for r in result.results if r.status == "failed"]
    actual = {}
    for field_name in EXPECTED:
        field = fields[field_name]
        actual[field_name] = await service._read_field_value(field)
    assert actual == EXPECTED, (name, actual)
    assert await page.locator('[name="emergency_name"]').input_value() == ""
    assert not await page.locator('[name="relocate"]:checked').count()
    assert await page.locator("body").get_attribute("data-submitted") is None
    assert await page.locator("body").get_attribute("data-wrong") is None
    after = await service.pre_submit_check(name)
    next_decision = await decide_application_step(state, await service.snapshot(), PROFILE, after, use_model=False)
    assert next_decision.next_action == "review_before_submit" and not next_decision.can_execute
    await context.close()
    return {"case": name, "adapter": adapter, "matched_by": snapshot.site_route.matched_by,
            "stage_checks": 2, "stage_correct": 2, "eligible_fields": len(EXPECTED),
            "planned_fields": planned, "correct_fields": correct, "verified_fields": result.verified,
            "manual_questions": 2, "manual_handoffs": 2, "incorrect_writes": 0}


async def adversarial_options(browser) -> dict:
    context = await browser.new_context()
    page = await context.new_page()
    await context.route("**/*", lambda route: route.fulfill(content_type="text/html", body="<body></body>"))
    await page.goto("https://careers.example.test/apply")
    service = BrowserDemoService()
    service.page, service.context, service.session_id = page, context, "options"
    # A broken ARIA relationship must not borrow an unrelated visible city's option.
    await page.set_content('''<label>期望工作城市<button type="button" role="combobox" name="city"
      aria-controls="missing-menu">请选择</button></label>
      <div role="listbox"><div role="option" onclick="document.body.dataset.wrong='yes'">北京市</div></div>''')
    snapshot = await service.snapshot()
    field = next(f for f in snapshot.fields if f.name == "city")
    assert field.options == []
    request = ExecutePlanRequest(actions=[FillAction(selector=field.selector, label=field.label,
                                      action="select", value="北京市", confidence=1)])
    result = await service.execute("options", request)
    assert result.failed == 1 and await page.locator("body").get_attribute("data-wrong") is None
    # An unlinked portal is usable when opening this control creates exactly
    # one new menu, even if an unrelated menu was already visible.
    await page.set_content('''<div class="form-field"><label>期望工作城市</label>
      <button type="button" role="combobox" name="city" onclick="document.getElementById('portal').hidden=false">请选择</button></div>
      <div id="portal" role="listbox" hidden><div role="option"
        onclick="document.querySelector('[name=city]').setAttribute('aria-valuetext','北京市');this.parentElement.hidden=true">北京市</div></div>
      <div role="listbox"><div role="option" onclick="document.body.dataset.wrong='yes'">上海市</div></div>
      <script>document.addEventListener('keydown',e=>{if(e.key==='Escape')document.getElementById('portal').hidden=true})</script>''')
    snapshot = await service.snapshot()
    field = next(f for f in snapshot.fields if f.name == "city")
    assert field.options == ["北京市"]
    result = await service.execute("options", ExecutePlanRequest(actions=[
        FillAction(selector=field.selector, label=field.label, action="select", value="北京市", confidence=1)]))
    assert result.verified == 1 and result.failed == 0
    assert await page.locator("body").get_attribute("data-wrong") is None
    # Identical captions in one linked menu require the user to resolve hierarchy.
    await page.set_content('''<label>期望工作城市<button type="button" role="combobox" name="city"
      aria-controls="menu">请选择</button></label><div id="menu" role="listbox">
      <div role="option" onclick="document.body.dataset.wrong='yes'">北京市</div>
      <div role="option" onclick="document.body.dataset.wrong='yes'">北京市</div></div>''')
    snapshot = await service.snapshot()
    field = next(f for f in snapshot.fields if f.name == "city")
    result = await service.execute("options", ExecutePlanRequest(actions=[
        FillAction(selector=field.selector, label=field.label, action="select", value="北京市", confidence=1)]))
    assert result.failed == 1 and await page.locator("body").get_attribute("data-wrong") is None
    await context.close()
    return {"option_scenarios": 3, "incorrect_writes": 0, "blocked_cases": 2, "unlinked_portal_verified": 1}


async def main() -> None:
    mapping_checks = verify_mapping_boundaries()
    routing_cases = [
        ("https://join.qq.com.evil.test", [], "generic"),
        ("https://notlever.co", [], "generic"),
        ("https://jobs.example.test", [".ant-form-item", ".el-form-item"], "generic"),
        ("https://jobs.example.test", [".moka-form-item"], "generic"),
        ("https://jobs.example.test", [".moka-form-item", ".moka-select", ".beisen-form-item", ".beisen-select"], "generic"),
        ("https://jobs.example.zhiye.com", [], "beisen-italent"),
    ]
    for url, markers, expected in routing_cases:
        assert resolve_site_route(url, markers).adapter == expected
    async with async_playwright() as playwright:
        channel = os.getenv("APP_BROWSER_CHANNEL", "chrome")
        browser = await playwright.chromium.launch(channel=channel, headless=True)
        try:
            with patch("app.application_agent.configured_model", side_effect=RuntimeError("offline evaluation")):
                cases = [await run_case(browser, case) for case in CASES]
                adversarial = await adversarial_options(browser)
        finally:
            await browser.close()
    eligible = sum(case["eligible_fields"] for case in cases)
    planned = sum(case["planned_fields"] for case in cases)
    correct = sum(case["correct_fields"] for case in cases)
    report = {"fixture_type": "synthetic_offline_not_live_sites", "model_calls": 0,
              "routing_checks": len(routing_cases), "mapping_boundary_checks": mapping_checks,
              "cases": cases, "adversarial": adversarial,
              "metrics": {"stage_accuracy": sum(c["stage_correct"] for c in cases) / sum(c["stage_checks"] for c in cases),
                          "mapping_precision": correct / planned if planned else None,
                          "automatic_coverage": planned / eligible,
                          "readback_success_rate": sum(c["verified_fields"] for c in cases) / planned,
                          "manual_question_rate": sum(c["manual_questions"] for c in cases) / (eligible + sum(c["manual_questions"] for c in cases)),
                          "incorrect_writes": sum(c["incorrect_writes"] for c in cases) + adversarial["incorrect_writes"]}}
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
