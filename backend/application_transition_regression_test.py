"""Negative journey regressions: no real browser, model, credentials or DB."""
import asyncio
from unittest.mock import patch

from app.application_agent import _allowed_actions, decide_application_step
from app.application_models import ApplicationWorkflowState
from app.browser_models import BrowserSnapshot, PageField, PreSubmitCheck
from app.models import CandidateProfile


URL = "https://fixture.example.test/application/one"
PROFILE = CandidateProfile(name="合成候选人")


def snapshot(fields):
    return BrowserSnapshot(session_id="transition-fixture",url=URL,title="合成申请表",fields=fields)


def workflow(stage="application_form"):
    return ApplicationWorkflowState(session_id="transition-fixture",url=URL,title="合成申请表",
        stage=stage,form_fields=2,safe_next_present=True,safe_next_label="下一步")


async def run():
    problems=[]
    matched=PageField(selector="#name",label="姓名",current_value=PROFILE.name)
    optional=PageField(selector="#contact",label="紧急联系人姓名",required=False)
    page=snapshot([matched,optional])
    check=PreSubmitCheck(url=URL,ready=True)
    allowed=_allowed_actions(workflow(),check,page,PROFILE)
    decision=await decide_application_step(workflow(),page,PROFILE,check,use_model=False)
    if "continue_application" in allowed or decision.can_execute:
        problems.append("未确认的非必填联系人被自动翻页跳过")
    if not any("紧急联系人" in question for question in decision.user_questions):
        problems.append("用户没有收到需要处理的联系人问题")

    # Identity gates already have an authoritative state and no automatic
    # action. A slow provider must not delay the human login/OTP prompt.
    for stage in ("auth_required","registration_required","verification_required"):
        with patch("app.application_agent.configured_model",side_effect=RuntimeError("synthetic slow gateway")) as provider:
            outcome=await decide_application_step(workflow(stage),snapshot([]),PROFILE,None)
        assert outcome.requires_user and not outcome.can_execute
        if provider.call_count:
            problems.append(f"{stage} 等待模型后才提示本人操作")
    if problems:
        for problem in problems:
            print("REPRODUCED:",problem)
        raise AssertionError("; ".join(problems))

    # Safe fully reviewed continuation is preserved.
    clean=await decide_application_step(workflow(),snapshot([matched]),PROFILE,check,use_model=False)
    assert clean.next_action=="continue_application" and clean.can_execute
    # Known empty values still get filled before human-only questions.
    pending=snapshot([matched.model_copy(update={"current_value":""}),optional])
    fill=await decide_application_step(workflow(),pending,PROFILE,check,use_model=False)
    assert fill.next_action=="analyze_and_fill" and fill.can_execute
    print("application_transition_regression_test: OK (optional human facts survive page boundaries; immediate login/OTP prompts; safe fill/next preserved)")


if __name__=="__main__":asyncio.run(run())
