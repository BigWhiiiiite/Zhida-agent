"""Product orchestration contract: no network, storage, browser or credentials."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from app.application_journey import continue_journey
from app.application_models import (ApplicationAgentCheckpoint, ApplicationAgentDecision,
    ApplicationAgentTurn, ApplicationWorkflowState)
from app.browser_models import ApplicationAssistResult, BrowserSnapshot, PageField, PreSubmitCheck, RequiredFieldIssue
from app.form_agent import build_form_review, create_local_form_plan
from app.models import CandidateProfile


def turn(stage, action="", next_action="stop", *, assisted=None, ready=False, can_execute=False, value="", url=None):
    snapshot=BrowserSnapshot(session_id="journey-fixture",url=url or "https://fixture.example.test/"+stage,
        title="匿名测试",fields=[PageField(selector="#field",label="合成字段",current_value=value)])
    return ApplicationAgentTurn(
        snapshot=snapshot,workflow=ApplicationWorkflowState(session_id=snapshot.session_id,url=snapshot.url,
            title=snapshot.title,stage=stage,form_fields=int(stage in {"application_form","review"}),
            final_submit_present=stage=="review"),
        decision=ApplicationAgentDecision(stage=stage,goal="推进",summary="合成状态",next_action=next_action,
            next_label="合成步骤",rationale="fixture",can_execute=can_execute,requires_user=not can_execute),
        action_taken=action,pre_submit=PreSubmitCheck(url=snapshot.url,ready=ready),
        assistance=ApplicationAssistResult(status=assisted,message="合成补齐结果",snapshot=snapshot) if assisted else None,
        checkpoint=ApplicationAgentCheckpoint(run_id="fixture",session_id=snapshot.session_id,status="active",
            stage=stage,url=snapshot.url,title=snapshot.title,updated_at=datetime.now(timezone.utc)))


async def run():
    for stage,status in (("auth_required","waiting_login"),("registration_required","waiting_registration"),
                         ("verification_required","waiting_verification")):
        steps=[]
        async def step():
            steps.append(1)
            return turn(stage,"start_application")
        result=await continue_journey(max_steps=4,run_step=step,guard=lambda:None)
        assert result.status==status and len(steps)==1

    # After human login, resume actual fresh state; do not replay login or send OTP.
    sequence=[turn("application_form","start_application","analyze_and_fill",can_execute=True),
              turn("application_form","analyze_and_fill","continue_application",assisted="ready_for_review",
                   ready=True,can_execute=True,value="known"),
              turn("review","continue_application","review_before_submit",ready=True)]
    async def step():
        return sequence.pop(0)
    result=await continue_journey(max_steps=4,run_step=step,guard=lambda:None)
    assert result.status=="ready_for_review" and result.steps==3 and not sequence
    assert [event.action for event in result.events]==["start_application","analyze_and_fill","continue_application"]

    # A visible final button is not proof all information is filled.
    async def missing():return turn("review",next_action="review_before_submit",ready=False)
    assert (await continue_journey(max_steps=4,run_step=missing,guard=lambda:None)).status=="needs_user"
    async def declarations():return turn("review","analyze_and_fill",assisted="ready_for_review",ready=False)
    assert (await continue_journey(max_steps=4,run_step=declarations,guard=lambda:None)).status=="ready_for_review"
    # Required-field completeness is not a substitute for reviewing unknown
    # optional facts. Return a usable question instead of false green status.
    optional = turn("review",ready=True)
    optional.snapshot.fields = [PageField(selector="#birth",label="出生日期",field_type="date")]
    optional.review = build_form_review(optional.snapshot,create_local_form_plan(optional.snapshot,CandidateProfile()))
    async def unknown_optional():return optional
    assert (await continue_journey(max_steps=4,run_step=unknown_optional,guard=lambda:None)).status=="needs_user"
    stale = turn("review","analyze_and_fill",assisted="ready_for_review",ready=True)
    stale.snapshot = stale.snapshot.model_copy(deep=True)
    stale.snapshot.fields[0].current_value = "changed-after-check"
    async def changed_page():return stale
    assert (await continue_journey(max_steps=4,run_step=changed_page,guard=lambda:None)).status=="partial"
    invalid = turn("review","analyze_and_fill",assisted="ready_for_review",ready=False)
    invalid.pre_submit.validation_errors = ["校验失败"]
    async def fresh_error():return invalid
    assert (await continue_journey(max_steps=4,run_step=fresh_error,guard=lambda:None)).status=="needs_user"
    invalid.pre_submit.validation_errors = []
    invalid.pre_submit.required_missing = [RequiredFieldIssue(selector="#field",label="缺失资料",field_type="text")]
    assert (await continue_journey(max_steps=4,run_step=fresh_error,guard=lambda:None)).status=="needs_user"
    for status in ("needs_user","blocked","partial"):
        async def unfinished():return turn("application_form","analyze_and_fill",assisted=status)
        assert (await continue_journey(max_steps=4,run_step=unfinished,guard=lambda:None)).status==status

    calls=[]
    async def stuck():
        calls.append(1)
        return turn("job_list","browse_jobs","browse_jobs",can_execute=True)
    result=await continue_journey(max_steps=6,run_step=stuck,guard=lambda:None)
    assert result.status=="partial" and len(calls)==2
    async def refresh():return turn("job_list","refresh","refresh",can_execute=True)
    assert (await continue_journey(max_steps=6,run_step=refresh,guard=lambda:None)).steps==1
    calls=[]
    async def many():
        calls.append(1)
        return turn("job_list","browse_jobs","browse_jobs",can_execute=True,value=str(len(calls)))
    result=await continue_journey(max_steps=3,run_step=many,guard=lambda:None)
    assert result.status=="partial" and len(calls)==3
    calls=[]
    def stale():
        if calls:raise ValueError("Changed revision")
    try:
        await continue_journey(max_steps=4,run_step=many,guard=stale)
        raise AssertionError("Stale journey continued")
    except ValueError:
        assert len(calls)==1
    print("application_journey_test: OK (login/registration/OTP handoffs, resume, safe multi-page, questions, no-progress/step/revision bounds)")


if __name__=="__main__":asyncio.run(run())
