"""Synthetic guard tests for grounded form mapping and exact option execution."""

import asyncio
import sys
from unittest.mock import AsyncMock, patch

from app.browser_service import _best_option, _option_matches, _selected_values_match
from app.browser_models import BrowserSnapshot, FillAction, FormPlan, PageField
from app.models import CandidateProfile, Education


def page_with(*fields: PageField) -> BrowserSnapshot:
    return BrowserSnapshot(session_id="synthetic-hybrid", url="https://example.test/form",
                           title="Synthetic application", fields=list(fields))


def field_at(selector: str, question: str, **updates) -> PageField:
    return PageField(**{"selector": selector, "question_text": question, "label": question,
                        "label_source": "explicit", "required": True, **updates})


def proposal_for(field: PageField, **updates) -> FillAction:
    return FillAction(**{"selector": field.selector, "label": "model-generated label",
        "action": "fill", "value": "invented@example.test", "value_source": "claimed-source",
        "confidence": .99, "profile_path": "email", "question_evidence": "本人电子邮箱", **updates})


async def mapped_plan(page: BrowserSnapshot, profile: CandidateProfile,
                      proposals: list[FillAction]) -> tuple[FormPlan, AsyncMock]:
    from app import form_agent

    runner = AsyncMock(return_value=FormPlan(actions=proposals))
    with patch.dict("os.environ", {"APP_AGENT_MODEL": "fixture", "APP_AGENT_FALLBACK_MODEL": "",
                                  "APP_AGENT_PROMPT_JSON_MODELS": ""}), \
            patch.object(form_agent, "configured_model", return_value=None), \
            patch.object(form_agent, "_structured_plan", runner):
        return await form_agent.create_form_plan(page, profile), runner


async def test_grounded_routing() -> None:
    from app import form_agent
    from app.form_routing import grounded_model_action

    profile = CandidateProfile(name="示例姓名", english_name="Example Candidate", age=25,
        email="confirmed@example.test", target_cities=["北京"], location="上海",
        education=[Education(degree="本科", school="示例本科大学", college="示例本科学院"),
                   Education(degree="硕士", school="示例硕士大学", college="示例硕士学院")])
    known = page_with(field_at("#name", "姓名"), field_at("#english", "英文姓名"), field_at("#age", "年龄"))
    with patch.object(form_agent, "configured_model", side_effect=AssertionError("known facts must not call model")):
        local = await form_agent.create_form_plan(known, profile)
    assert [action.value for action in local.actions] == [profile.name, profile.english_name, "25"]
    assert local.routing_summary.rules_ready == 3

    unclear = field_at("#notify", "通知接收地址", context="请填写本人电子邮箱", label_source="nearby")
    page = page_with(unclear)
    routed = form_agent.create_local_form_plan(page, profile)
    assert routed.actions[0].needs_model and routed.actions[0].action == "ask_user"
    assert routed.missing_questions == [], "a model-pending field should not ask the user yet"
    result, runner = await mapped_plan(page, profile, [proposal_for(unclear)])
    assert runner.await_count == 1
    action = result.actions[0]
    assert action.action == "fill" and action.value == profile.email
    assert action.value != "invented@example.test" and action.value_source == "主档案.email"
    assert action.review_question == "本人电子邮箱" and action.resolution_source == "model"
    assert result.routing_summary.model_resolved == 1 and result.routing_summary.needs_user == 0

    for updates in ({"question_evidence": "不存在于网页的邮箱问题"},
                    {"profile_path": "email.__class__"}, {"profile_path": "education[0].school"},
                    {"profile_path": "phone"}, {"confidence": .89}):
        result, _ = await mapped_plan(page, profile, [proposal_for(unclear, **updates)])
        assert result.actions[0].action == "ask_user" and not result.actions[0].value, updates
        assert not result.actions[0].needs_model
        assert result.actions[0].review_question and result.actions[0].review_hint
    result, _ = await mapped_plan(page, profile.model_copy(update={"email": ""}), [proposal_for(unclear)])
    assert result.actions[0].action == "ask_user" and "主档案" in result.actions[0].review_hint

    # Nearby siblings are supplementary context, not proof of this field's own question.
    neighbour = field_at("#other", "通知接收地址", nearby_labels=["本人电子邮箱"], label_source="nearby")
    result, _ = await mapped_plan(page_with(neighbour), profile, [proposal_for(neighbour)])
    assert result.actions[0].action == "ask_user"

    # Missing known facts, credentials, consent and third-party data are not a
    # reason to send the same question to a model that cannot know the answer.
    protected = page_with(field_at("#phone", "手机号"), field_at("#contact", "紧急联系人姓名"),
        field_at("#consent", "是否同意隐私协议", field_type="checkbox"),
        field_at("#code", "手机验证码"), field_at("#unknown", "未识别字段 5", label_source="generated", section="基本信息", ordinal=5))
    with patch.object(form_agent, "configured_model", side_effect=AssertionError("protected fields must not call model")):
        protected_plan = await form_agent.create_form_plan(protected, profile)
    assert all(not action.needs_model for action in protected_plan.actions)
    assert protected_plan.actions[0].action == protected_plan.actions[1].action == "ask_user"
    assert protected_plan.actions[3].action == "skip"
    assert "基本信息" in protected_plan.actions[4].review_question
    assert "原题未完整读取" in protected_plan.actions[4].review_question

    # Explicit degree groups bind an entire record. A model may recover a
    # missing label, but never swap the bachelor's college for the master's.
    degree_field = field_at("#college", "培养单位部门", context="硕士所属学院", section="硕士教育经历", label_source="nearby")
    result, _ = await mapped_plan(page_with(degree_field), profile, [proposal_for(degree_field,
        profile_path="education.college", entity_scope="education:master", question_evidence="硕士所属学院")])
    assert result.actions[0].action == "fill" and result.actions[0].value == "示例硕士学院"
    explicit_bachelor = field_at("#bachelor", "所属学院", section="本科教育经历", context="本科所属学院")
    rejected = grounded_model_action(proposal_for(explicit_bachelor,
        profile_path="education.college", entity_scope="education:master", question_evidence="本科所属学院"),
        explicit_bachelor, page_with(explicit_bachelor), profile)
    assert rejected.action == "ask_user" and "层次" in rejected.review_hint
    same_level_duplicate = profile.model_copy(update={"education": [
        *profile.education, Education(degree="硕士", college="第二段示例硕士学院")]})
    result, _ = await mapped_plan(page_with(degree_field), same_level_duplicate, [proposal_for(degree_field,
        profile_path="education.college", entity_scope="education:master", question_evidence="硕士所属学院")])
    assert result.actions[0].action == "ask_user"

    # A model claim of confidence cannot turn a compound city option into one city.
    city = field_at("#work", "任职地点", context="期望工作城市", field_type="select-one",
                    options=["北京/上海"], label_source="nearby")
    result, _ = await mapped_plan(page_with(city), profile, [proposal_for(city, profile_path="target_cities",
        question_evidence="期望工作城市", action="select", value="北京/上海")])
    assert result.actions[0].action == "ask_user"

    # Transient model failures leave a readable, bounded manual handoff.
    with patch.dict("os.environ", {"APP_AGENT_MODEL": "fixture", "APP_AGENT_FALLBACK_MODEL": "",
                                  "APP_AGENT_PROMPT_JSON_MODELS": ""}), \
            patch.object(form_agent, "configured_model", return_value=None), \
            patch.object(form_agent, "_structured_plan", AsyncMock(side_effect=RuntimeError("offline fixture"))):
        failed = await form_agent.create_form_plan(page, profile)
    assert failed.actions[0].action == "ask_user" and not failed.actions[0].needs_model
    assert failed.routing_summary.needs_user == 1


def test_option_boundaries() -> None:
    negative = (
        ("北京", "北京/上海"), ("北京", "非北京地区"), ("1", "10年"),
        ("计算机", "非计算机专业"), ("本科", "本科及以上"),
        ("C", "C++"), ("C", "C#"), ("AB", "A/B"),
        ("远程", "远程面试（需服从线下面试安排）"),
        ("示例学院", "另一所示例学院"),
        ("硕士", "研究生"), ("硕士", "硕士及以上"),
        ("硕士", "博士研究生"), ("博士", "博士后"), ("本科", "学士"),
    )
    for wanted, candidate in negative:
        assert not _option_matches(wanted, candidate), (wanted, candidate)
        assert _best_option(wanted, [candidate]) == "", (wanted, candidate)
        assert not _selected_values_match([wanted], candidate), (wanted, candidate)
    for wanted, candidate in (("北京", "北京市"), ("内蒙古", "内蒙古自治区"),
                               ("男", "Male"), ("中国", "中国大陆"),
                               ("英语", "English"), ("远程", "线上面试"),
                               ("硕士", "硕士研究生"), ("博士", "博士研究生"),
                               ("Python", "PYTHON"), ("Ｃ＋＋", "C++")):
        assert _option_matches(wanted, candidate), (wanted, candidate)
        assert _best_option(wanted, [candidate]) == candidate
        assert _selected_values_match([wanted], candidate)
    assert _best_option("北京市", ["北京市", "北京市"]) == ""
    assert _best_option("北京", ["北京市", "北京省"]) == ""
    assert _best_option("硕士", ["硕士研究生", "硕 士 研 究 生"]) == ""
    assert _best_option("硕士", ["硕士", "硕士研究生"]) == "硕士"
    assert not _selected_values_match(["北京"], "北京, 上海")
    assert not _selected_values_match(["北京", "上海"], "北京")
    assert not _selected_values_match(["北京", "北京"], "北京, 上海")
    assert _selected_values_match(["北京", "上海"], "上海市, 北京市")


async def test_owned_dom_labels() -> None:
    from playwright.async_api import async_playwright
    from app.browser_service import BrowserDemoService

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", lambda route: route.abort())
            await page.set_content("""<form>
              <div class="form-field">
                <label for="salutation">称呼</label><input id="salutation">
                <span class="field-label">姓名</span>
              </div>
              <div class="form-field">
                <span id="own-preference">联系偏好</span>
                <input id="preference" aria-labelledby="own-preference">
                <span class="field-label">手机号</span>
              </div>
              <div class="ant-form-item">
                <div class="ant-form-item-label">期望工作城市</div>
                <div class="ant-select-selector" id="owned-city"><input readonly></div>
              </div>
              <div class="form-field">
                <span class="field-label">联系方式</span>
                <input id="ambiguous-one"><input id="ambiguous-two">
              </div>
              <div class="form-field">
                <span class="field-label">外层问题</span><input id="outer-control">
                <div class="form-field"><span class="field-label">嵌套内层问题</span><input id="inner-control"></div>
              </div>
              <fieldset><legend>是否接受其他城市调剂</legend>
                <label><input type="radio" name="relocate" value="yes">是</label>
                <label><input type="radio" name="relocate" value="no">否</label>
              </fieldset>
            </form>""")
            service = BrowserDemoService()
            service.page, service.session_id = page, "synthetic-owned-labels"
            snapshot = await service.snapshot()
            salutation = next(field for field in snapshot.fields if field.name == "salutation")
            assert salutation.question_text == "称呼", salutation.model_dump()
            assert salutation.label_source == "explicit"
            preference = next(field for field in snapshot.fields if field.name == "preference")
            assert preference.question_text == "联系偏好", preference.model_dump()
            assert preference.label_source == "aria-labelledby"
            owned_city = next(field for field in snapshot.fields if field.name == "owned-city")
            assert owned_city.question_text == "期望工作城市", owned_city.model_dump()
            assert owned_city.label_source == "container-owned", owned_city.model_dump()
            ambiguous = [field for field in snapshot.fields if field.name in {"ambiguous-one", "ambiguous-two", "outer-control"}]
            assert len(ambiguous) == 3 and all(field.label_source != "container-owned" for field in ambiguous), [field.model_dump() for field in ambiguous]
            inner = next(field for field in snapshot.fields if field.name == "inner-control")
            assert inner.question_text == "嵌套内层问题" and inner.label_source == "container-owned", inner.model_dump()
            radio = [field for field in snapshot.fields if field.name == "relocate"]
            assert radio and all(field.question_text == "是否接受其他城市调剂" for field in radio)
            assert all(field.label_source == "explicit" for field in radio), [field.model_dump() for field in radio]
        finally:
            await browser.close()


if __name__ == "__main__":
    test_option_boundaries()
    asyncio.run(test_grounded_routing())
    if "--browser" in sys.argv:
        asyncio.run(test_owned_dom_labels())
    print("hybrid mapping tests passed: grounded values, routing, questions, degree isolation, option boundaries")
