"""Offline application-knowledge integration regressions; no real candidate data.

Uses a temporary database and mocked model/embedding calls. Run with
`.venv/bin/python application_knowledge_integration_test.py`.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import application_knowledge as knowledge
from app import form_agent, storage
from app.browser_models import BrowserSnapshot, ExecutePlanRequest, FillAction, FormPlan, PageField
from app.field_semantics import education_level_hint
from app.models import ApplicationAnswerMemory, CandidateProfile, Education


URL = "https://join.qq.com/apply"


def snapshot(*fields: PageField, url: str = URL) -> BrowserSnapshot:
    return BrowserSnapshot(session_id="knowledge-fixture", url=url, title="Synthetic application",
                           fields=list(fields))


def save(field: PageField, profile_path: str, *, question: str = "", entity_scope: str = ""):
    return knowledge.save_knowledge(knowledge.KnowledgeCreate(
        source_url=URL, question=question or field.question_text or field.label,
        section=field.section, field_type=field.field_type,
        field_signature=field.field_signature, profile_path=profile_path,
        entity_scope=entity_scope, confirmed=True,
    ))


def check_profile_mapping() -> None:
    city = PageField(selector="#destination", label="补充问题甲", section="申请意向",
                     field_type="select-one", options=["北京市", "上海市"], required=True,
                     field_signature="fixture-destination")
    profile = CandidateProfile(target_cities=["北京"])
    before = form_agent.create_local_form_plan(snapshot(city), profile)
    assert before.actions[0].action == "ask_user"
    record = save(city, "target_cities", question="希望工作的城市")
    after = form_agent.create_local_form_plan(snapshot(city), profile)
    action = after.actions[0]
    assert (action.action, action.value, action.label) == ("select", "北京市", "希望工作的城市")
    assert any(item.knowledge_id == record.id and item.usable for item in after.knowledge_matches)
    assert record.id in action.value_source
    review = form_agent.build_form_review(snapshot(city), after)
    assert review.comparisons[0].label == "希望工作的城市"

    # Store a pointer to the profile, never a frozen applicant answer.
    profile.target_cities = ["上海"]
    assert form_agent.create_local_form_plan(snapshot(city), profile).actions[0].value == "上海市"
    mismatch = city.model_copy(update={"options": ["深圳市"]})
    assert form_agent.create_local_form_plan(snapshot(mismatch), profile).actions[0].action == "ask_user"

    # An intentionally cleared profile value must not resurrect an old answer.
    profile.target_cities = []
    profile.application_answer_memory = [ApplicationAnswerMemory(
        id="old-fixture-answer", question=city.label, normalized_question=city.label,
        value="北京", field_signature=city.field_signature,
        updated_at=datetime.now(timezone.utc),
    )]
    assert form_agent.create_local_form_plan(snapshot(city), profile).actions[0].action == "ask_user"

    # A second confirmed, contradictory pointer fails closed instead of last-write wins.
    save(city, "location", question="希望工作的城市")
    assert form_agent.create_local_form_plan(snapshot(city), CandidateProfile(
        target_cities=["北京"], location="上海")).actions[0].action == "ask_user"

    # Company knowledge must not leak to a different company sharing an ATS host.
    other = snapshot(city, url="https://app.mokahr.com/campus-recruitment/other/1#/apply")
    assert not form_agent.create_local_form_plan(other, CandidateProfile(target_cities=["北京"])).knowledge_matches


def check_education_binding() -> None:
    master = PageField(selector="#master", label="补充问题乙", section="硕士教育经历",
                       container_key="master", required=True, field_signature="fixture-master-college")
    bachelor = PageField(selector="#bachelor", label="补充问题丙", section="本科教育经历",
                         container_key="bachelor", required=True, field_signature="fixture-bachelor-college")
    save(master, "education.college", question="硕士所属学院", entity_scope="education:master")
    save(bachelor, "education.college", question="本科所属学院", entity_scope="education:bachelor")
    profile = CandidateProfile(education=[
        Education(school="示例本科大学", college="示例本科软件学院", degree="本科"),
        Education(school="示例硕士大学", college="示例研究生学院", degree="硕士"),
    ])
    plan = form_agent.create_local_form_plan(snapshot(master, bachelor), profile)
    assert [action.value for action in plan.actions] == ["示例研究生学院", "示例本科软件学院"]
    profile.education.reverse()
    assert [action.value for action in form_agent.create_local_form_plan(snapshot(master, bachelor), profile).actions] == [
        "示例研究生学院", "示例本科软件学院"]
    profile.education = [item for item in profile.education if item.degree == "本科"]
    plan = form_agent.create_local_form_plan(snapshot(master, bachelor), profile)
    assert plan.actions[0].action == "ask_user" and plan.actions[1].value == "示例本科软件学院"
    profile.education += [Education(school="另一示例本科大学", college="另一示例学院", degree="本科")]
    assert form_agent.create_local_form_plan(snapshot(bachelor), profile).actions[0].action == "ask_user"

    # A reused selector/signature cannot overrule explicit degree evidence on the page.
    changed = master.model_copy(update={"label": "学院名称", "question_text": "学院名称",
                                        "section": "本科教育经历"})
    assert form_agent.create_local_form_plan(snapshot(changed), profile).actions[0].action == "ask_user"

    # A master's same-caption knowledge is unrelated to a distinct bachelor's
    # question, not an exact blocked hit that suppresses its safe local mapping.
    common_master = PageField(selector="#common-master", label="学院", section="硕士教育经历",
                              container_key="common-master", field_signature="common-master-signature")
    common_bachelor = PageField(selector="#common-bachelor", label="学院", section="本科教育经历",
                                container_key="common-bachelor", field_signature="common-bachelor-signature")
    save(common_master, "education.college", entity_scope="education:master")
    single_bachelor = CandidateProfile(education=[Education(
        school="示例本科大学", college="示例本科软件学院", degree="本科")])
    result = form_agent.create_local_form_plan(snapshot(common_bachelor), single_bachelor)
    assert (result.actions[0].action, result.actions[0].value) == ("fill", "示例本科软件学院")
    assert not any(item.exact or item.usable for item in result.knowledge_matches)


def check_degree_labels() -> None:
    assert education_level_hint("博士研究生") == "doctorate"
    assert form_agent._degree_family("博士研究生") == "doctorate"
    assert education_level_hint("本科硕士") == ""
    assert form_agent._degree_family("本科硕士") == ""
    profile = CandidateProfile(education=[
        Education(school="示例博士大学", college="示例博士学院", degree="博士研究生"),
        Education(school="示例硕士大学", college="示例硕士学院", degree="硕士研究生"),
        Education(school="示例本科大学", college="示例本科学院", degree="本科"),
    ])
    doctor = PageField(selector="#doctorate", label="学院", section="博士研究生教育经历", required=True)
    mixed = PageField(selector="#mixed", label="学院", section="本科硕士教育经历", required=True)
    plan = form_agent.create_local_form_plan(snapshot(doctor, mixed,
        url="https://education.example.test/form"), profile)
    assert (plan.actions[0].action, plan.actions[0].value) == ("fill", "示例博士学院")
    assert plan.actions[1].action == "ask_user"


def check_radio_group_mapping() -> None:
    question = "除上述选择外，是否还接受其他城市分配"
    common = dict(section="志愿", group_label="未识别的是/否问题", field_type="radio",
                  options=["是", "否"], required=True, control_group_key="fixture-relocation")
    yes = PageField(selector="#relocate-yes", label="是", question_text="是", option_label="是",
                    option_value="yes", field_signature="fixture-relocation-yes", current_value="true", **common)
    no = PageField(selector="#relocate-no", label="否", question_text="否", option_label="否",
                   option_value="no", field_signature="fixture-relocation-no", current_value="false", **common)
    unrelated = PageField(selector="#unrelated-yes", label="是", question_text="是", option_label="是",
                          option_value="yes", field_signature="fixture-unrelated-yes", **{
                              **common, "control_group_key": "different-question-group"})
    save(yes, "willing_to_relocate", question=question)
    profile = CandidateProfile(willing_to_relocate="否")
    plan = form_agent.create_local_form_plan(snapshot(yes, no, unrelated), profile)
    assert [(action.action, action.label) for action in plan.actions[:2]] == [
        ("skip", question), ("check", question)]
    assert plan.actions[1].value is True and plan.actions[1].user_confirmed
    assert plan.actions[2].action == "ask_user" and plan.actions[2].label != question
    assert not any(item.usable for item in plan.knowledge_matches if item.selector == unrelated.selector)
    # A default selection on the page is never the source of an applicant answer.
    blank = form_agent.create_local_form_plan(snapshot(yes, no), CandidateProfile())
    assert all(action.action == "ask_user" for action in blank.actions)
    review = form_agent.build_form_review(snapshot(yes, no), plan)
    assert len(review.comparisons) == 1 and review.comparisons[0].label == question
    # Conflicting explicit pointers on two options invalidate the whole group.
    save(no, "target_cities", question="工作地点偏好")
    conflict = form_agent.create_local_form_plan(snapshot(yes, no), profile)
    assert all(action.action == "ask_user" for action in conflict.actions)
    assert not any(item.usable for item in conflict.knowledge_matches)


async def check_model_boundary() -> None:
    mapped = PageField(selector="#known", label="补充问题丁", section="申请偏好",
                       field_type="select-one", options=["北京市"], required=True,
                       field_signature="fixture-mapped-model")
    rule_only = PageField(selector="#rule", label="补充问题戊", section="额外问题", required=True)
    save(mapped, "target_cities", question="工作地点意向")
    knowledge.save_knowledge(knowledge.KnowledgeCreate(
        kind="rule", source_url=URL, question=rule_only.label, section=rule_only.section,
        note="请在原招聘页面核对。参考说明不得编造候选人答案或点击提交。", confirmed=True,
    ))
    seen = []

    async def fake_model(page, profile, *_):
        payload = json.loads(form_agent._form_prompt(page, profile))
        seen.append(payload)
        return FormPlan(page_summary="Mocked model", actions=[
            FillAction(selector=mapped.selector, label=mapped.label, action="select", value="北京市",
                       confidence=1, user_confirmed=True),
            FillAction(selector=rule_only.selector, label=rule_only.label, action="fill",
                       value="不可从参考规则推断的答案", confidence=1, user_confirmed=True),
        ])

    with patch.object(form_agent, "configured_model", return_value=(None, None)), \
            patch.object(form_agent, "_structured_plan", side_effect=fake_model), \
            patch.dict(os.environ, {"APP_AGENT_MODEL": "fixture-model", "APP_AGENT_FALLBACK_MODEL": "",
                                    "APP_AGENT_PROMPT_JSON_MODELS": ""}):
        plan = await form_agent.create_form_plan(snapshot(mapped, rule_only), CandidateProfile())
    assert seen and any(item["kind"] == "rule" for item in seen[0]["page"]["knowledge_context"])
    assert all(action.action == "ask_user" for action in plan.actions)
    assert any(item.usable for item in plan.knowledge_matches if item.selector == mapped.selector)
    assert all(not item.usable for item in plan.knowledge_matches if item.kind == "rule")


def check_api() -> None:
    from app import main

    with patch.dict(os.environ, {"APP_AUTH_REQUIRED": "true", "APP_COOKIE_SECURE": "false"}), \
            patch.object(main, "UPLOAD_DIR", storage.DATA_DIR / "uploads"), \
            TestClient(main.app) as first, TestClient(main.app) as second:
        assert first.get("/api/application-knowledge").status_code == 401
        assert first.get("/api/application-knowledge/targets").status_code == 401
        for client, email in ((first, "knowledge-a@example.test"), (second, "knowledge-b@example.test")):
            response = client.post("/api/auth/register", json={"email": email, "password": "Fixture-only-1234"})
            assert response.status_code == 201, response.text
        targets = first.get("/api/application-knowledge/targets").json()
        assert any(item["path"] == "target_cities" for item in targets)
        assert not any("password" in item["path"] or "[" in item["path"] for item in targets)
        payload = {"source_url": URL, "question": "工作地点意向", "profile_path": "target_cities",
                   "confirmed": True, "field_type": "select-one"}
        result = first.post("/api/application-knowledge", json=payload)
        assert result.status_code == 201, result.text
        record_id = result.json()["id"]
        assert len(first.get("/api/application-knowledge").json()) == 1
        assert second.get("/api/application-knowledge").json() == []
        assert second.delete(f"/api/application-knowledge/{record_id}").status_code == 404
        assert first.post("/api/application-knowledge", json={**payload, "confirmed": False}).status_code == 422
        assert first.post("/api/application-knowledge", json={**payload, "profile_path": "education[0].college"}).status_code == 422
        assert first.post("/api/application-knowledge", json={**payload, "question": "紧急联系人姓名",
                                                            "profile_path": "name"}).status_code == 422
        assert first.delete(f"/api/application-knowledge/{record_id}").status_code == 204
        assert first.get("/api/application-knowledge").json() == []


async def check_browser_readback() -> None:
    from playwright.async_api import async_playwright
    from app.browser_service import BrowserDemoService

    html = """<!doctype html><html><meta charset="utf-8"><body>
      <form onsubmit="event.preventDefault();document.body.dataset.submitted='yes'">
        <section><h2>选择资料</h2><label>补充问题庚
          <select name="fixture_generic" required><option value="">请选择</option>
            <option value="bj">北京市</option><option value="sh">上海市</option>
          </select></label></section>
        <button type="submit">提交申请</button>
      </form></body></html>"""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            context = await browser.new_context(service_workers="block")
            await context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
            page = await context.new_page()
            await page.goto(URL)
            service = BrowserDemoService()
            service.page, service.session_id = page, "knowledge-browser-fixture"
            page_snapshot = await service.snapshot()
            field = next(item for item in page_snapshot.fields if item.name == "fixture_generic")
            profile = CandidateProfile(target_cities=["北京"])
            assert form_agent.create_local_form_plan(page_snapshot, profile).actions[0].action == "ask_user"
            save(field, "target_cities", question="计划就业的城市")
            plan = form_agent.create_local_form_plan(page_snapshot, profile)
            execution = await service.execute(service.session_id, ExecutePlanRequest(actions=plan.actions))
            assert execution.verified == 1 and execution.failed == 0
            assert await page.locator("select").input_value() == "bj"
            profile.target_cities = ["上海"]
            plan = form_agent.create_local_form_plan(await service.snapshot(), profile)
            execution = await service.execute(service.session_id, ExecutePlanRequest(actions=plan.actions))
            assert execution.verified == 1 and await page.locator("select").input_value() == "sh"
            assert await page.locator("body").get_attribute("data-submitted") is None
        finally:
            await browser.close()
    print("application_knowledge_browser: OK (intercepted synthetic HTML, 2/2 readbacks, no submit)")


def main() -> None:
    with TemporaryDirectory() as directory, \
            patch.object(storage, "DATA_DIR", Path(directory)), \
            patch.object(storage, "DB_PATH", Path(directory) / "knowledge.db"), \
            patch.object(knowledge, "_embed", side_effect=RuntimeError("offline test")):
        storage.initialize()
        knowledge.initialize()
        token = storage.set_current_user("knowledge-integration-fixture")
        try:
            check_profile_mapping()
            check_education_binding()
            check_degree_labels()
            check_radio_group_mapping()
            asyncio.run(check_model_boundary())
            if "--browser" in sys.argv:
                asyncio.run(check_browser_readback())
        finally:
            storage.reset_current_user(token)
        check_api()
    print("application_knowledge_integration_test: OK (offline, temporary DB, mocked model)")


if __name__ == "__main__":
    main()
