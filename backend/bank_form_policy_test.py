"""Anonymous fixtures copied from bank question wording, not applicant data.

No API, environment secrets, SQLite, existing browser or recruitment network.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.application_assist import is_declaration, safe_actions
from app.application_models import ApplicationWorkflowState
from app.browser_models import (BrowserSnapshot, ExecutePlanRequest, FillAction, FormPlan,
                               FormReviewResult, PageField, PreSubmitCheck)
from app.browser_service import BrowserDemoService
from app.field_semantics import entity_scope_for, semantic_key_for
from app.form_agent import create_local_form_plan
from app.form_field_policy import family_subject, formal_employment_only
from app.form_routing import grounded_model_action
from app.models import CandidateProfile, Education, Experience
from app.repeated_records import resolve_repeated_records


def field(question, **extra):
    return PageField(selector="#fixture", label=question, question_text=question,
                     label_source="explicit", required=True, **extra)


def page(*fields):
    return BrowserSnapshot(session_id="offline-bank", url="https://bank.example.test/form",
                           title="匿名银行申请表", fields=list(fields))


def run():
    profile = CandidateProfile(name="合成候选人", email="synthetic@example.test",
        education=[Education(school="合成本科大学", degree="本科"),
                   Education(school="合成硕士大学", degree="硕士")],
        internships=[Experience(organization="合成实习单位", employment_type="实习", role="实习生")])
    with patch("dotenv.load_dotenv", side_effect=AssertionError("dotenv forbidden")), \
         patch("sqlite3.connect", side_effect=AssertionError("SQLite forbidden")):
        for question in ("父亲姓名", "母亲姓名", "父亲学历", "母亲学历", "配偶单位",
                         "亲属手机", "Father name", "Mother education"):
            item = field(question, semantic_key="candidate.name", entity_scope="candidate")
            assert family_subject(item) and semantic_key_for(item) == "third_party.contact", question
            action = create_local_form_plan(page(item), profile).actions[0]
            assert action.action == "ask_user" and not action.value and not action.needs_model, question
            proposal = FillAction(selector=item.selector, label=question, action="fill", value=profile.name,
                                 confidence=1, profile_path="name", question_evidence=question)
            assert grounded_model_action(proposal, item, page(item), profile).action == "ask_user"
        for question in ("学历", "姓名", "职务"):
            assert family_subject(field(question, section="父亲资料"))
            assert entity_scope_for(field(question, section="父亲资料", entity_scope="candidate")) == "third_party:father"
            assert entity_scope_for(field(question, section="母亲资料", entity_scope="candidate")) == "third_party:mother"
        # Other people's captions in neighbouring context must not poison this
        # field's own identity, and a generic form section alone is not a family.
        item = field("姓名", context="姓名 父亲姓名 母亲姓名", section="个人基本信息")
        assert not family_subject(item)
        assert create_local_form_plan(page(item), profile).actions[0].value == profile.name
        assert not family_subject(field("本人学历", section="家庭成员及社会关系"))

        work = field("工作单位", section="工作经验", semantic_key="experience.organization",
                     container_key="formal:1", help_text="请填写正式签订劳动合同并缴纳社保的全职工作经历，实习经历请勿填写于此处。")
        assert formal_employment_only(work)
        assert not resolve_repeated_records(page(work), profile)
        action = create_local_form_plan(page(work), profile).actions[0]
        assert action.action == "ask_user" and not action.value and not action.needs_model
        proposal = FillAction(selector=work.selector, label="工作单位", action="fill", confidence=1,
                             profile_path="name", question_evidence="工作单位")
        assert grounded_model_action(proposal, work, page(work), profile).action == "ask_user"
        assert not formal_employment_only(field("实习单位", section="学生实践经验", context=work.help_text))
        assert not formal_employment_only(field("姓名", section="个人信息", context=work.help_text))

        for kind in ("combobox", "select-one", "radio", "checkbox", "text", "textarea"):
            item = field("本人承诺以上内容真实有效，同意银行对相关信息进行调查核实", field_type=kind)
            assert is_declaration(item), kind
            action = FillAction(selector=item.selector, label=item.label,
                action="select" if kind in {"combobox", "select-one"} else "check" if kind in {"radio", "checkbox"} else "fill",
                value="确认", confidence=1, user_confirmed=True)
            review = FormReviewResult(snapshot=page(item), plan=FormPlan(actions=[action]))
            assert not safe_actions(review), kind
        for question in ("是否服从岗位调剂", "Do you agree to relocate?", "是否获得资格证书 certification"):
            assert not is_declaration(field(question, field_type="combobox")), question
    print("bank_form_policy_test: OK (family subjects, formal work, all declaration controls)")


async def execution_gate():
    items = [field("父亲姓名"),
             field("工作单位", section="正式工作经历"),
             field("本人承诺以上内容真实有效", field_type="combobox"),
             field("本人承诺以上内容真实有效", field_type="select-one")]
    for index, item in enumerate(items):
        item.selector = f"#fixture-{index}"
    snapshot = page(*items)
    service = BrowserDemoService()
    async def evaluate(expression, arg=None):
        if "performance.timeOrigin" in expression:
            return 42 if arg is None else True
        return {}  # anonymous native-control identity inventory
    async def evaluate_handle(expression):
        assert expression == '() => document'
        async def same_document(script):
            assert ''.join(script.split()) == 'original=>original===document'
            return True  # this synthetic page never navigates or reloads
        return SimpleNamespace(evaluate=same_document, dispose=AsyncMock())
    service.page = SimpleNamespace(url=snapshot.url, is_closed=lambda: False,
        evaluate=evaluate, evaluate_handle=evaluate_handle, wait_for_timeout=AsyncMock())
    service.session_id = snapshot.session_id
    service.workflow_state = AsyncMock(return_value=ApplicationWorkflowState(
        session_id=snapshot.session_id, url=snapshot.url, title=snapshot.title,
        stage="application_form", form_fields=len(items)))
    service.snapshot = AsyncMock(return_value=snapshot)
    service.pre_submit_check = AsyncMock(return_value=PreSubmitCheck(url=snapshot.url))
    service._resolve_field = AsyncMock(side_effect=AssertionError("Protected values must never reach a browser writer"))
    actions = [FillAction(selector=item.selector, label=item.label,
        action="select" if item.field_type in {"combobox", "select-one"} else "fill",
        value="虚假候选人值", confidence=1, user_confirmed=index >= 2)
        for index, item in enumerate(items)]
    result = await service.execute(snapshot.session_id, ExecutePlanRequest(actions=actions))
    assert result.completed == result.failed == 0 and result.skipped == 4, result
    service._resolve_field.assert_not_awaited()
    print("bank executor gate: OK (forged confirmed declarations still cannot reach writer)")


if __name__ == "__main__":
    run()
    asyncio.run(execution_gate())
