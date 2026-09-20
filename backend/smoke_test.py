"""Run with: .venv/bin/python smoke_test.py"""
import asyncio
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from agents.usage import Usage
from docx import Document
from fastapi.testclient import TestClient
from openai import APIConnectionError

from app import main, storage
from app.agent import _profile_from_model_text
from app.ats_field_profiles import field_profile_for_url
from app.browser_models import BrowserSnapshot, PageField
from app.browser_service import _finalize_choice_metadata
from app.field_semantics import enrich_fields, option_fingerprint
from app.form_agent import build_form_review, _form_plan_from_model_text, _form_prompt, _local_safe_plan, _matching_option
from app.extractors import extract_text
from app.model_provider import normalize_proxy_response
from app.models import ApplicationAnswerMemory, CandidateProfile, Education, Experience, ModelHealth
from app.job_recommendations import recommendation_batch

os.environ["APP_AGENT_MODE"] = "rules"
os.environ["APP_AUTH_REQUIRED"] = "true"

assert field_profile_for_url("https://join.qq.com/apply")["name"] == "tencent-campus"
assert field_profile_for_url("https://app.mokahr.com/campus-recruitment/demo")["name"] == "moka-campus"
assert field_profile_for_url("https://jobs.example.com/apply")["name"] == "generic-semantic"


async def verify_proxy_normalization() -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "https://proxy.example/v1/responses"),
        json={
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 15,
            }
        },
    )
    await normalize_proxy_response(response)
    assert response.json()["usage"]["input_tokens_details"]["cache_write_tokens"] == 0


asyncio.run(verify_proxy_normalization())
assert Usage().input_tokens_details.cached_tokens == 0
assert logging.getLogger("openai.agents").level == logging.CRITICAL
fallback_profile = _profile_from_model_text(
    '```json\n{"name":"备用模型测试","email":"fallback@example.com"}\n```'
)
assert fallback_profile.name == "备用模型测试"
assert fallback_profile.email == "fallback@example.com"

fallback_plan = _form_plan_from_model_text(
    '```json\n{"page_summary":"test","site_type":"lever","actions":[],"missing_questions":[]}\n```'
)
assert fallback_plan.site_type == "lever"

local_plan = _local_safe_plan(
    BrowserSnapshot(session_id="test", url="https://jobs.lever.co/example/apply", title="Test", fields=[
        PageField(selector="[data-zhida-field=name]", label="Full name", name="name", required=True),
        PageField(selector="[data-zhida-field=email]", label="Email", name="email", required=True),
        PageField(selector="[data-zhida-field=gender]", label="Gender", name="gender", required=True),
        PageField(selector="[data-zhida-field=resume]", label="Resume/CV", name="resume", field_type="file", required=True),
    ]),
    CandidateProfile(name="Test Candidate", email="test@example.com"),
)
assert [action.action for action in local_plan.actions] == ["fill", "fill", "ask_user", "skip"]
assert local_plan.actions[2].sensitive

relationship_plan = _local_safe_plan(
    BrowserSnapshot(session_id="relationship", url="https://careers.example/apply", title="Test", fields=[
        PageField(selector="[data-zhida-field=emergency-name]", label="紧急联系人姓名", required=True),
        PageField(selector="[data-zhida-field=emergency-phone]", label="紧急联系人电话", required=True),
    ]),
    CandidateProfile(name="候选人本人", phone="13800138000"),
)
assert [action.action for action in relationship_plan.actions] == ["ask_user", "ask_user"]
assert all(action.sensitive and not action.value for action in relationship_plan.actions)

decision_fields = [
    PageField(selector="[data-zhida-field=transfer-yes]", label="是否接受调剂 — 是", name="transfer",
              field_type="radio", group_label="是否接受调剂", option_label="是", option_value="yes",
              options=["是", "否"], required=True),
    PageField(selector="[data-zhida-field=transfer-no]", label="是否接受调剂 — 否", name="transfer",
              field_type="radio", group_label="是否接受调剂", option_label="否", option_value="no",
              options=["是", "否"], required=True),
]
decision_plan = _local_safe_plan(
    BrowserSnapshot(session_id="decision", url="https://careers.example/apply", title="Test",
                    fields=decision_fields),
    CandidateProfile(),
)
assert [action.action for action in decision_plan.actions] == ["ask_user", "ask_user"]
remembered_decision = _local_safe_plan(
    BrowserSnapshot(session_id="decision", url="https://careers.example/apply", title="Test",
                    fields=decision_fields),
    CandidateProfile(application_answers={"是否接受调剂": "否"}),
)
assert [action.action for action in remembered_decision.actions] == ["skip", "check"]
assert remembered_decision.actions[1].user_confirmed

opaque_choice_rows = [
    {"selector": "[data-zhida-field=opaque-yes]", "label": "是", "label_source": "aria",
     "context": "是 是", "ordinal": 8, "name": "", "field_type": "radio", "required": True,
     "options": ["是", "否"], "group_label": "是", "option_label": "是", "option_value": "yes",
     "control_group_key": "opaque-group"},
    {"selector": "[data-zhida-field=opaque-no]", "label": "否", "label_source": "aria",
     "context": "否 否", "ordinal": 9, "name": "", "field_type": "radio", "required": True,
     "options": ["是", "否"], "group_label": "否", "option_label": "否", "option_value": "no",
     "control_group_key": "opaque-group"},
]
_finalize_choice_metadata(opaque_choice_rows)
assert len({row["group_label"] for row in opaque_choice_rows}) == 1
assert opaque_choice_rows[0]["group_label"].startswith("未识别的是/否问题")
opaque_choice_plan = _local_safe_plan(
    (opaque_choice_snapshot := BrowserSnapshot(
        session_id="opaque-choice", url="https://join.qq.com/apply", title="Test",
        fields=[PageField.model_validate(row) for row in opaque_choice_rows],
    )),
    CandidateProfile(application_answers={opaque_choice_rows[0]["group_label"]: "否"}),
)
assert [action.action for action in opaque_choice_plan.actions] == ["ask_user", "ask_user"]
assert all("阻止" in action.reason for action in opaque_choice_plan.actions)
opaque_choice_review = build_form_review(opaque_choice_snapshot, opaque_choice_plan)
assert len(opaque_choice_review.comparisons) == 1
assert opaque_choice_review.comparisons[0].options == ["是", "否"]

education_plan = _local_safe_plan(
    BrowserSnapshot(session_id="education", url="https://careers.example/apply", title="Test", fields=[
        PageField(selector="[data-zhida-field=level]", label="最高学历", field_type="select-one",
                  options=["高中", "大学专科", "大学本科", "硕士研究生"]),
        PageField(selector="[data-zhida-field=degree]", label="学位", field_type="select-one",
                  options=["学士", "硕士", "博士"]),
    ]),
    CandidateProfile(education=[Education(school="Test University", degree="本科")]),
)
assert [action.value for action in education_plan.actions] == ["大学本科", "学士"]
assert _matching_option("male", ["female"]) == ""

multi_education_profile = CandidateProfile(education=[
    Education(school="示例硕士大学", college="示例研究生学院", degree="硕士", major="机器学习"),
    Education(school="示例本科大学", college="示例本科软件学院", degree="本科", major="计算机科学与技术"),
])
scoped_education_fields = enrich_fields([
    PageField(selector="[data-zhida-field=master-school]", label="院校名称", section="硕士教育经历",
              container_key="master-block", required=True),
    PageField(selector="[data-zhida-field=master-college]", label="学院名称", section="硕士教育经历",
              container_key="master-block", required=True),
    PageField(selector="[data-zhida-field=bachelor-school]", label="院校名称", section="本科教育经历",
              container_key="bachelor-block", required=True),
    PageField(selector="[data-zhida-field=bachelor-college]", label="学院名称", section="本科教育经历",
              container_key="bachelor-block", required=True),
], "https://careers.example/apply")
scoped_education_plan = _local_safe_plan(
    BrowserSnapshot(session_id="education-scoped", url="https://careers.example/apply", title="Test",
                    fields=scoped_education_fields),
    multi_education_profile,
)
scoped_values = {action.selector: action.value for action in scoped_education_plan.actions}
assert scoped_values["[data-zhida-field=master-school]"] == "示例硕士大学"
assert scoped_values["[data-zhida-field=master-college]"] == "示例研究生学院"
assert scoped_values["[data-zhida-field=bachelor-school]"] == "示例本科大学"
assert scoped_values["[data-zhida-field=bachelor-college]"] == "示例本科软件学院"
assert all("已将本组锁定" in action.reason for action in scoped_education_plan.actions)

ambiguous_education_fields = enrich_fields([
    PageField(selector="[data-zhida-field=unknown-school]", label="院校名称", section="教育经历",
              container_key="unknown-block", required=True),
    PageField(selector="[data-zhida-field=unknown-college]", label="学院名称", section="教育经历",
              container_key="unknown-block", required=True),
], "https://careers.example/apply")
ambiguous_education_plan = _local_safe_plan(
    BrowserSnapshot(session_id="education-ambiguous", url="https://careers.example/apply", title="Test",
                    fields=ambiguous_education_fields),
    multi_education_profile,
)
assert all(action.action == "ask_user" for action in ambiguous_education_plan.actions)
assert all("不能在以下记录中猜测" in action.reason for action in ambiguous_education_plan.actions)

mixed_site_fields = [
    field.model_copy(update={"current_value": value})
    for field, value in zip(ambiguous_education_fields, ["示例硕士大学", "示例本科软件学院"])
]
mixed_site_plan = _local_safe_plan(
    BrowserSnapshot(session_id="education-mixed", url="https://careers.example/apply", title="Test",
                    fields=mixed_site_fields), multi_education_profile,
)
assert all(action.action == "ask_user" for action in mixed_site_plan.actions)
assert all("串填风险" in action.reason for action in mixed_site_plan.actions)

remembered_education_profile = multi_education_profile.model_copy(deep=True)
remembered_education_profile.application_answer_memory = [ApplicationAnswerMemory(
    id="education-memory", question="院校名称", normalized_question="院校名称",
    semantic_key="education.school", entity_scope="education:unspecified",
    field_signature=ambiguous_education_fields[0].field_signature, field_type="text",
    value="示例硕士大学", source_host="careers.example", confirmed_count=1,
    updated_at="2026-09-14T00:00:00Z",
)]
remembered_education_plan = _local_safe_plan(
    BrowserSnapshot(session_id="education-memory", url="https://careers.example/apply", title="Test",
                    fields=ambiguous_education_fields), remembered_education_profile,
)
assert remembered_education_plan.actions[0].action == "fill"
assert remembered_education_plan.actions[0].value == "示例硕士大学"
assert "复用你在" in remembered_education_plan.actions[0].reason
assert remembered_education_plan.actions[1].action == "ask_user"

remembered_field = enrich_fields([
    PageField(selector="[data-zhida-field=rotation]", label="是否接受轮岗", field_type="select-one",
              options=["是", "否"], required=True),
], "https://careers.example/apply")[0]
remembered_profile = CandidateProfile(application_answer_memory=[ApplicationAnswerMemory(
    id="memory-1", question="是否接受轮岗", normalized_question="是否接受轮岗",
    semantic_key=remembered_field.semantic_key, entity_scope=remembered_field.entity_scope,
    field_signature=remembered_field.field_signature, field_type=remembered_field.field_type,
    option_fingerprint="67ca096d7b92c097", value="否", source_host="careers.example",
    confirmed_count=2, updated_at="2026-09-14T00:00:00Z",
)])
# Use the actual fingerprint so the saved option set must remain compatible.
remembered_profile.application_answer_memory[0].option_fingerprint = option_fingerprint(remembered_field.options)
remembered_plan = _local_safe_plan(
    BrowserSnapshot(session_id="remembered", url="https://careers.example/apply", title="Test",
                    fields=[remembered_field]), remembered_profile,
)
assert remembered_plan.actions[0].action == "select"
assert remembered_plan.actions[0].value == "否"
assert "已确认 2 次" in remembered_plan.actions[0].reason
changed_options_field = remembered_field.model_copy(update={"options": ["接受", "不接受"]})
changed_options_plan = _local_safe_plan(
    BrowserSnapshot(session_id="changed-options", url="https://careers.example/apply", title="Test",
                    fields=[changed_options_field]), remembered_profile,
)
assert changed_options_plan.actions[0].action == "ask_user"
assert "选项" in changed_options_plan.actions[0].reason

legacy_sensitive_plan = _local_safe_plan(
    BrowserSnapshot(session_id="legacy-sensitive", url="https://careers.example/apply", title="Test", fields=[
        PageField(selector="[data-zhida-field=id-number]", label="证件号码（用于实名认证）",
                  label_source="explicit", required=True),
    ]),
    CandidateProfile(application_answers={"证件号码（用于实名认证）": "should-not-be-reused"}),
)
assert legacy_sensitive_plan.actions[0].action == "ask_user"
assert legacy_sensitive_plan.actions[0].sensitive
assert not legacy_sensitive_plan.actions[0].value
safe_model_prompt = _form_prompt(
    BrowserSnapshot(session_id="prompt", url="https://careers.example/apply", title="Test", fields=[]),
    CandidateProfile(
        application_answers={"技术社区": "GitHub", "证件号码": "secret-id-value"},
        application_answer_memory=[ApplicationAnswerMemory(
            id="secret-memory", question="普通问题", normalized_question="普通问题",
            value="memory-only-value", updated_at="2026-09-14T00:00:00Z",
        )],
    ),
)
assert "GitHub" in safe_model_prompt
assert "secret-id-value" not in safe_model_prompt
assert "memory-only-value" not in safe_model_prompt

ambiguous_experience_plan = _local_safe_plan(
    BrowserSnapshot(session_id="experience-ambiguous", url="https://careers.example/apply", title="Test", fields=[
        PageField(selector="[data-zhida-field=intern-company]", label="请输入实习公司",
                  label_source="explicit"),
    ]),
    CandidateProfile(
        internships=[Experience(organization="示例研发机构", role="Agent 实习生"),
                     Experience(organization="示例科技公司", role="AI 实习生")],
        application_answers={"请输入实习公司": "示例研发机构"},
    ),
)
assert ambiguous_experience_plan.actions[0].action == "ask_user"
assert "不能在以下记录中猜测" in ambiguous_experience_plan.actions[0].reason

semantic_profile = CandidateProfile(
    gender="男", country_region="中国", location="北京", target_cities=["北京"],
    preferred_business_groups=["TEG"], interview_preferences=["远程面试"],
    willing_to_relocate="否",
    education=[Education(school="Test University", degree="本科", location="北京市", current=True)],
    skills=["Python", "Agent"], languages=["英语"],
)
semantic_fields = [
    PageField(selector="[data-zhida-field=gender-male]", label="男", field_type="radio",
              group_label="性别", option_label="男", option_value="male", options=["男", "女"], required=True),
    PageField(selector="[data-zhida-field=gender-female]", label="女", field_type="radio",
              group_label="性别", option_label="女", option_value="female", options=["男", "女"], required=True),
    PageField(selector="[data-zhida-field=country]", label="国家/地区", field_type="select-one",
              options=["中国大陆", "新加坡"], required=True),
    PageField(selector="[data-zhida-field=current-location]", label="当前所处地", field_type="combobox",
              options=["北京市", "上海市"], required=True),
    PageField(selector="[data-zhida-field=work-city]", label="期望工作城市", field_type="combobox",
              options=["北京", "深圳"], required=True),
    PageField(selector="[data-zhida-field=interview-city]", label="参加面试城市", field_type="combobox",
              options=["远程面试"], required=True),
    PageField(selector="[data-zhida-field=study-location]", label="目前就读地", field_type="combobox",
              options=["北京市", "上海市"], required=True),
    PageField(selector="[data-zhida-field=skills]", label="AI应用技能", field_type="select-multiple",
              options=["Python", "Agent", "Java"], multiple=True),
    PageField(selector="[data-zhida-field=languages]", label="语言能力", field_type="textarea"),
    PageField(selector="[data-zhida-field=business-group]", label="感兴趣的事业群",
              field_type="combobox", options=["TEG", "WXG"], required=True),
    PageField(selector="[data-zhida-field=relocation-yes]", label="是", field_type="radio",
              group_label="除上述选择外，是否还接受其他城市分配", option_label="是",
              option_value="yes", options=["是", "否"], required=True, control_group_key="relocation"),
    PageField(selector="[data-zhida-field=relocation-no]", label="否", field_type="radio",
              group_label="除上述选择外，是否还接受其他城市分配", option_label="否",
              option_value="no", options=["是", "否"], required=True, control_group_key="relocation"),
]
semantic_plan = _local_safe_plan(
    BrowserSnapshot(session_id="semantic", url="https://careers.example/apply", title="Test",
                    fields=semantic_fields),
    semantic_profile,
)
semantic_actions = {action.selector: action for action in semantic_plan.actions}
assert semantic_actions["[data-zhida-field=gender-male]"].action == "check"
assert semantic_actions["[data-zhida-field=gender-male]"].value is True
assert semantic_actions["[data-zhida-field=gender-female]"].action == "skip"
assert semantic_actions["[data-zhida-field=country]"].value == "中国大陆"
assert semantic_actions["[data-zhida-field=current-location]"].value == "北京市"
assert semantic_actions["[data-zhida-field=work-city]"].value == "北京"
assert semantic_actions["[data-zhida-field=interview-city]"].action == "select"
assert semantic_actions["[data-zhida-field=interview-city]"].value == "远程面试"
assert semantic_actions["[data-zhida-field=study-location]"].value == "北京市"
assert semantic_actions["[data-zhida-field=skills]"].value == "Python, Agent"
assert semantic_actions["[data-zhida-field=languages]"].value == "英语"
assert semantic_actions["[data-zhida-field=business-group]"].value == "TEG"
assert semantic_actions["[data-zhida-field=relocation-yes]"].action == "skip"
assert semantic_actions["[data-zhida-field=relocation-no]"].action == "check"

context_only_plan = _local_safe_plan(
    BrowserSnapshot(session_id="context-only", url="https://careers.example/apply", title="Test", fields=[
        PageField(selector="[data-zhida-field=context-country]", label="未识别字段 1",
                  label_source="generated", context="国家/地区 请选择", field_type="combobox",
                  options=["中国大陆", "新加坡"], required=True),
    ]),
    semantic_profile,
)
assert context_only_plan.actions[0].action == "select"
assert context_only_plan.actions[0].value == "中国大陆"

review_fields = [field.model_copy(deep=True) for field in semantic_fields]
review_fields[0].current_value = "true"
review_fields[2].current_value = "中国大陆"
review_fields[4].current_value = "深圳"
review_fields[5].current_value = "远程面试"
review_fields[7].current_value = "Python"
review_fields[8].current_value = "英语"
review_snapshot = BrowserSnapshot(
    session_id="review", url="https://careers.example/apply", title="Test", fields=review_fields,
)
review_plan = _local_safe_plan(review_snapshot, semantic_profile)
review = build_form_review(review_snapshot, review_plan)
review_by_label = {item.label: item for item in review.comparisons}
assert review_by_label["性别"].status == "matched"
assert review_by_label["国家/地区"].status == "matched"
assert review_by_label["当前所处地"].status == "missing"
assert review_by_label["期望工作城市"].status == "conflict"
assert review_by_label["参加面试城市"].status == "matched"
assert review_by_label["AI应用技能"].status == "conflict"
assert review_by_label["语言能力"].status == "matched"
assert next(action for action in review.plan.actions
            if action.selector == "[data-zhida-field=country]").action == "skip"

checkbox_fields = [
    PageField(selector=f"[data-zhida-field=skill-{name.lower()}]", label=name,
              field_type="checkbox", group_label="AI应用技能", option_label=name,
              current_value="true" if name == "Python" else "false")
    for name in ("Python", "Agent", "Java")
]
checkbox_snapshot = BrowserSnapshot(
    session_id="checkbox", url="https://careers.example/apply", title="Test", fields=checkbox_fields,
)
checkbox_plan = _local_safe_plan(checkbox_snapshot, semantic_profile)
assert [action.action for action in checkbox_plan.actions] == ["check", "check", "skip"]
checkbox_review = build_form_review(checkbox_snapshot, checkbox_plan)
assert checkbox_review.comparisons[0].status == "conflict"
assert checkbox_review.comparisons[0].site_value == "Python"
assert checkbox_review.comparisons[0].expected_value == "Python, Agent"

missing_country_plan = _local_safe_plan(
    BrowserSnapshot(session_id="country", url="https://careers.example/apply", title="Test",
                    fields=[semantic_fields[2]]),
    CandidateProfile(),
)
assert missing_country_plan.actions[0].action == "ask_user"
assert missing_country_plan.actions[0].reason.endswith("请从网页真实选项中选择")

learned_plan = _local_safe_plan(
    BrowserSnapshot(session_id="learned", url="https://careers.example/apply", title="Test", fields=[
        PageField(selector="[data-zhida-field=qq]", label="QQ号", name="qq", required=True),
        PageField(selector="[data-zhida-field=community]", label="你最常参与的技术社区", name="community"),
    ]),
    CandidateProfile(qq="12345678", application_answers={"你最常参与的技术社区": "GitHub"}),
)
assert [action.value for action in learned_plan.actions] == ["12345678", "GitHub"]
assert learned_plan.actions[0].value_source == "主档案.qq"
assert learned_plan.actions[1].value_source.startswith("主档案.application_answers")

graduating_profile = CandidateProfile(
    target_role="Agent 开发工程师", skills=["Python", "FastAPI", "Agent"],
    education=[Education(school="Test University", end_date="2026.12")],
)
graduating_jobs = recommendation_batch(graduating_profile).jobs
baidu_agent = next(item for item in graduating_jobs if item.job.job_code == "J101017")
assert baidu_agent.graduation_match is True

shenzhen_batch = recommendation_batch(graduating_profile, "深圳市")
assert shenzhen_batch.selected_location == "深圳"
assert "深圳" in shenzhen_batch.available_locations
assert shenzhen_batch.jobs
assert all(item.location_match is True for item in shenzhen_batch.jobs)
assert all(any("深圳" in location for location in item.job.locations) for item in shenzhen_batch.jobs)
assert all(any("所选城市：深圳" in reason for reason in item.reasons) for item in shenzhen_batch.jobs)


class UnavailableExtractor:
    name = "unavailable-test-provider"

    async def parse(self, _: str):
        raise APIConnectionError(request=httpx.Request("POST", "https://proxy.example/v1/responses"))


with TemporaryDirectory() as temporary:
    root = Path(temporary)
    merged_docx = root / "merged.docx"
    document = Document()
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).merge(table.cell(1, 0)).text = "重复标题"
    table.cell(0, 1).text = "第一行"
    table.cell(1, 1).text = "第二行"
    document.save(merged_docx)
    assert extract_text(merged_docx).count("重复标题") == 1

    storage.DB_PATH = root / "test.db"
    main.UPLOAD_DIR = root / "uploads"

    with TestClient(main.app) as client:
        assert client.get("/api/health").json()["product"] == "Zhida"
        assert "/api/jobs/{job_id}/verify" in client.get("/openapi.json").json()["paths"]
        assert "/api/jobs/sources" in client.get("/openapi.json").json()["paths"]
        assert "/api/jobs/sources/{source_id}/sync" in client.get("/openapi.json").json()["paths"]
        os.environ["APP_AUTH_REQUIRED"] = "false"
        local_me = client.get("/api/auth/me")
        assert local_me.status_code == 200
        assert local_me.json()["is_local"] is True
        assert client.get("/api/profile").status_code == 200
        os.environ["APP_AUTH_REQUIRED"] = "true"
        assert client.get("/api/profile").status_code == 401
        registered = client.post("/api/auth/register", json={
            "email": "alice@example.com", "password": "Alice-pass-2026", "display_name": "Alice",
        })
        assert registered.status_code == 201, registered.text
        assert registered.json()["user"]["email"] == "alice@example.com"
        assert "password" not in registered.text.lower()
        assert "httponly" in registered.headers["set-cookie"].lower()
        assert client.get("/api/auth/me").json()["display_name"] == "Alice"
        original_health_check = main.check_model_health
        main.check_model_health = lambda: asyncio.sleep(0, result=ModelHealth(
            status="ok", model="test-model", latency_ms=12, message="主模型当前可用"
        ))
        try:
            model_health = client.post("/api/model/health")
            assert model_health.status_code == 200
            assert model_health.json()["status"] == "ok"
            assert "key" not in model_health.text.lower()
        finally:
            main.check_model_health = original_health_check
        initial = client.get("/api/profile")
        assert initial.status_code == 200

        remembered_qq = client.post("/api/profile/application-answer", json={
            "question": "QQ号", "field_name": "candidate_qq", "value": "12345678",
        })
        assert remembered_qq.status_code == 200
        assert remembered_qq.json()["qq"] == "12345678"
        remembered_custom = client.post("/api/profile/application-answer", json={
            "question": "你最常参与的技术社区", "field_name": "community", "value": "GitHub",
            "semantic_key": "application.custom", "entity_scope": "application",
            "field_signature": "test-community-signature", "field_type": "text",
            "source_url": "https://careers.example/apply",
        })
        assert remembered_custom.status_code == 200
        assert remembered_custom.json()["application_answers"]["你最常参与的技术社区"] == "GitHub"
        assert remembered_custom.json()["application_answer_memory"][0]["field_signature"] == "test-community-signature"
        assert remembered_custom.json()["application_answer_memory"][0]["source_host"] == "careers.example"
        remembered_preference = client.post("/api/profile/application-answer", json={
            "question": "感兴趣的事业群", "field_name": "business_group", "value": "TEG, CSIG",
            "semantic_key": "preference.business_group", "entity_scope": "preference",
            "field_signature": "test-business-group", "field_type": "select-multiple",
            "options": ["TEG", "CSIG", "WXG"], "source_url": "https://join.qq.com/apply",
        })
        assert remembered_preference.status_code == 200, remembered_preference.text
        assert remembered_preference.json()["preferred_business_groups"] == ["TEG", "CSIG"]
        rejected_sensitive = client.post("/api/profile/application-answer", json={
            "question": "是否同意隐私条款", "field_name": "consent", "value": "是",
        })
        assert rejected_sensitive.status_code == 422
        rejected_identity = client.post("/api/profile/application-answer", json={
            "question": "证件号码（用于实名认证）", "field_name": "identity_number",
            "value": "should-not-be-stored",
        })
        assert rejected_identity.status_code == 422

        first_text = (
            "姓名：李春博\n性别：男\n年龄：24\n邮箱：first@example.com\n"
            "手机号：13800138000\n技能\nPython，FastAPI\n项目经历\nAgent 求职助手"
        ).encode()
        first = client.post("/api/resumes", files={"file": ("agent.txt", first_text, "text/plain")})
        assert first.status_code == 201, first.text
        first_json = first.json()
        assert first_json["profile"]["name"] == "李春博"
        assert first_json["evidence"]

        assert client.post("/api/auth/logout").status_code == 204
        bob = client.post("/api/auth/register", json={
            "email": "bob@example.com", "password": "Bob-pass-2026", "display_name": "Bob",
        })
        assert bob.status_code == 201, bob.text
        assert client.get("/api/resumes").json() == []
        assert client.get("/api/profile").json()["email"] == ""
        assert client.get(f"/api/resumes/{first_json['id']}").status_code == 404
        assert client.post("/api/auth/logout").status_code == 204
        assert client.post("/api/auth/login", json={
            "email": "alice@example.com", "password": "incorrect-password",
        }).status_code == 401
        signed_in = client.post("/api/auth/login", json={
            "email": "ALICE@example.com", "password": "Alice-pass-2026",
        })
        assert signed_in.status_code == 200, signed_in.text
        assert client.get("/api/resumes").json()[0]["id"] == first_json["id"]

        duplicate = client.post("/api/resumes", files={"file": ("copy.txt", first_text, "text/plain")})
        assert duplicate.status_code == 409

        profile = client.get("/api/profile").json()
        assert profile["email"] == "first@example.com"

        recommendations = client.get("/api/jobs/recommendations")
        assert recommendations.status_code == 200
        recommendation_json = recommendations.json()
        assert recommendation_json["engine"] == "official-source-adapters-local-retrieval-v4"
        assert len(recommendation_json["jobs"]) >= 5
        scores = [item["match_score"] for item in recommendation_json["jobs"]]
        assert scores == sorted(scores, reverse=True)
        assert all(item["reasons"] for item in recommendation_json["jobs"])
        assert all(item["job"]["source_url"].startswith("https://") for item in recommendation_json["jobs"])
        assert all(item["job"]["company_size"] in {"large", "growth", "startup", "unknown"}
                   for item in recommendation_json["jobs"])

        source_list = client.get("/api/jobs/sources")
        assert source_list.status_code == 200 and len(source_list.json()) >= 4
        custom_source = client.post("/api/jobs/sources", json={
            "company": "API Test Startup", "official_url": "https://jobs.ashbyhq.com/api-test-startup",
            "adapter": "auto", "company_size": "startup",
        })
        assert custom_source.status_code == 201, custom_source.text
        custom_source_json = custom_source.json()
        assert custom_source_json["adapter"] == "ashby" and custom_source_json["user_added"]
        cached_search = client.post("/api/jobs/search", json={
            "query": "Agent", "location": "", "company_sizes": [], "sync_sources": False,
        })
        assert cached_search.status_code == 200, cached_search.text
        assert cached_search.json()["query"] == "Agent"
        assert cached_search.json()["synced_sources"] >= 4
        assert client.delete(f"/api/jobs/sources/{custom_source_json['id']}").status_code == 204
        assert client.delete("/api/jobs/sources/baidu-campus").status_code == 409

        shenzhen_recommendations = client.get("/api/jobs/recommendations", params={"location": "深圳"})
        assert shenzhen_recommendations.status_code == 200
        shenzhen_json = shenzhen_recommendations.json()
        assert shenzhen_json["selected_location"] == "深圳"
        assert shenzhen_json["jobs"]
        assert all("深圳" in item["job"]["locations"] for item in shenzhen_json["jobs"])

        first_job_id = recommendation_json["jobs"][0]["job"]["id"]
        queued = client.post("/api/jobs/queue", json={"job_ids": [first_job_id], "resume_id": first_json["id"]})
        assert queued.status_code == 201, queued.text
        assert queued.json()[0]["job_id"] == first_job_id
        assert queued.json()[0]["resume_id"] == first_json["id"]
        queue_id = queued.json()[0]["id"]
        assert client.get("/api/jobs/queue").json()[0]["recommendation"]["reasons"]
        readiness = client.get("/api/readiness")
        assert readiness.status_code == 200 and not readiness.json()["ready"]
        assert readiness.json()["resume_count"] == 1
        assert "主档案缺少教育经历" in readiness.json()["blockers"]
        unconfirmed = client.patch(
            f"/api/jobs/queue/{queue_id}", json={"status": "submitted"}
        )
        assert unconfirmed.status_code == 422
        progressing = client.patch(f"/api/jobs/queue/{queue_id}", json={
            "status": "in_progress", "notes": "等待补充开放题",
        })
        assert progressing.status_code == 200
        assert progressing.json()["status"] == "in_progress"
        assert progressing.json()["notes"] == "等待补充开放题"
        assert progressing.json()["assistance_started_at"]
        assert progressing.json()["confirmation_pending"] is True
        not_submitted = client.patch(f"/api/jobs/queue/{queue_id}", json={
            "status": "needs_review", "candidate_confirmed": True,
        })
        assert not_submitted.status_code == 200
        assert not_submitted.json()["confirmation_pending"] is False
        progressing = client.patch(f"/api/jobs/queue/{queue_id}", json={"status": "in_progress"})
        assert progressing.status_code == 200 and progressing.json()["confirmation_pending"] is True
        submitted = client.patch(f"/api/jobs/queue/{queue_id}", json={
            "status": "submitted", "application_id": "APP-2026-001",
            "candidate_confirmed": True,
        })
        assert submitted.status_code == 200
        assert submitted.json()["submitted_at"] and submitted.json()["application_id"] == "APP-2026-001"
        assert submitted.json()["confirmation_pending"] is False
        exported_queue = client.get("/api/jobs/queue-export.csv")
        assert exported_queue.status_code == 200
        assert "text/csv" in exported_queue.headers["content-type"]
        assert "APP-2026-001" in exported_queue.content.decode("utf-8-sig")
        protected_delete = client.delete(f"/api/jobs/queue/{queue_id}")
        assert protected_delete.status_code == 409
        withdrawn = client.patch(f"/api/jobs/queue/{queue_id}", json={"status": "withdrawn"})
        assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
        assert client.delete(f"/api/jobs/queue/{queue_id}").status_code == 204
        assert client.get("/api/jobs/queue").json() == []
        unknown_job = client.post("/api/jobs/queue", json={"job_ids": ["not-a-job"], "resume_id": ""})
        assert unknown_job.status_code == 422

        second_text = "姓名：李春博\n邮箱：new@example.com\n技能\nPydanticAI，React".encode()
        second = client.post("/api/resumes", files={"file": ("english.txt", second_text, "text/plain")})
        assert second.status_code == 201, second.text
        conflicts = client.get("/api/conflicts").json()
        email_conflict = next(item for item in conflicts if item["field_path"] == "email")
        resolved = client.post(f"/api/conflicts/{email_conflict['id']}/resolve", json={"choice": "incoming"})
        assert resolved.status_code == 200
        assert client.get("/api/profile").json()["email"] == "new@example.com"

        evidence_id = first_json["evidence"][0]["id"]
        reviewed = client.patch(
            f"/api/resumes/{first_json['id']}/evidence/{evidence_id}", json={"status": "confirmed"}
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["evidence"][0]["status"] == "confirmed"

        assert client.get(f"/api/resumes/{first_json['id']}/download").status_code == 200
        preview = client.get(f"/api/resumes/{first_json['id']}/preview")
        assert preview.status_code == 200
        assert "text/html" in preview.headers["content-type"]

        skills_evidence = next(item for item in first_json["evidence"] if item["field_path"] == "skills")
        edited = client.patch(
            f"/api/resumes/{first_json['id']}/evidence/{skills_evidence['id']}",
            json={"status": "edited", "value": ["Python", "React"]},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["profile"]["skills"] == ["Python", "React"]
        assert client.get("/api/profile").json()["skills"] == ["Python", "React"]

        assert client.post(f"/api/resumes/{first_json['id']}/parse").status_code == 200

        original_extractor = main.get_resume_extractor
        main.get_resume_extractor = lambda: UnavailableExtractor()
        try:
            failed_text = "姓名：网络故障测试\n邮箱：retry@example.com".encode()
            failed = client.post("/api/resumes", files={"file": ("retry.txt", failed_text, "text/plain")})
            assert failed.status_code == 503, failed.text
            failed_id = failed.json()["detail"]["resume_id"]
            retained = client.get(f"/api/resumes/{failed_id}").json()
            assert retained["status"] == "failed"
            assert retained["error_message"]
            assert (main.UPLOAD_DIR / f"{failed_id}.txt").exists()
        finally:
            main.get_resume_extractor = original_extractor
        retried = client.post(f"/api/resumes/{failed_id}/parse")
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "needs_review"

        assert client.get("/api/export").status_code == 200
        assert client.delete(f"/api/resumes/{first_json['id']}").status_code == 204
        assert client.get(f"/api/resumes/{first_json['id']}").status_code == 404

print("Zhida phase-one smoke test passed")
