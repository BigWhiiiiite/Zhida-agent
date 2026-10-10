"""Bounded model fallback, stale-page and context safety; all synthetic/offline."""
from __future__ import annotations

import asyncio
import os
from unittest.mock import patch

from application_assist_test import Browser
from app import application_assist as assist
from app.browser_models import ActionResult, ApplicationAssistRequest, FillAction, FormPlan, PageField
from app.execution_safety import ExecutionTargetChanged, execution_phase_label
from app.form_agent import _local_safe_plan
from app.form_routing import route_local_plan
from app.models import CandidateProfile


class UnclearBrowser(Browser):
    def __init__(self):
        super().__init__()
        self.values.update(name="合成测试人",country="中国",city="北京",intro="")
        self.options_loaded=True

    async def settled_snapshot(self,sid):
        snapshot=await super().settled_snapshot(sid)
        snapshot.fields.append(PageField(selector="#intro",label="介绍你的适合之处",question_text="介绍你的适合之处",
            label_source="explicit",field_type="textarea",required=True,current_value=self.values["intro"]))
        return snapshot


async def run():
    # Earlier widget causes survive a later navigation, but unknown exception
    # text (including candidate answers) never enters an interruption receipt.
    known_failure=ExecutionTargetChanged('synthetic navigation',results=[ActionResult(
        selector='#date',label='日期',status='failed',
        message='当前日历尚无经过验证的日期输入方式，请在官网核对；不会强写只读值 private-value')])
    assert '日历输入方式尚未支持' in known_failure.attempted_issues[0].message
    assert 'private-value' not in repr(known_failure.__dict__)
    unknown_failure=ExecutionTargetChanged('synthetic navigation',results=[ActionResult(
        selector='#date',label='日期',status='failed',message='private-error private-value')])
    assert 'private-error' not in repr(unknown_failure.__dict__)
    assert 'private-value' not in repr(unknown_failure.__dict__)
    phase_failure=ExecutionTargetChanged('synthetic navigation', current_label='民族', current_phase='open_popup')
    assert execution_phase_label('open_popup') in phase_failure.attempted_issues[0].message
    unknown_phase=ExecutionTargetChanged('synthetic navigation', current_label='民族', current_phase='private-stage-answer')
    assert 'private-stage-answer' not in repr(unknown_phase.__dict__)
    assert '执行阶段尚未分类' in unknown_phase.attempted_issues[0].message
    assert assist.is_declaration(PageField(selector="#agree",name="agreechk",field_type="checkbox"))
    for question in ("是否同意异地工作", "Do you agree to relocate?", "是否获得该资格证书 certification"):
        assert not assist.is_declaration(PageField(selector="#preference",label=question,field_type="radio"))
    with patch.dict(os.environ, {}, clear=True):
        assert assist.model_budget_seconds() == 315
    with patch.dict(os.environ, {"APP_FORM_MODEL_TIMEOUT_SECONDS":"240", "APP_FORM_FALLBACK_TIMEOUT_SECONDS":"120"}):
        assert assist.model_budget_seconds() == 375
    with patch.dict(os.environ, {"APP_FORM_MODEL_TIMEOUT_SECONDS":"9999", "APP_FORM_FALLBACK_TIMEOUT_SECONDS":"9999"}):
        assert assist.model_budget_seconds() == 600
    with patch.dict(os.environ, {"APP_FORM_MODEL_TIMEOUT_SECONDS":"nan", "APP_FORM_FALLBACK_TIMEOUT_SECONDS":"invalid"}):
        assert assist.model_budget_seconds() == 315
    profile=CandidateProfile(name="合成测试人",country_region="中国",target_cities=["北京"])
    request=ApplicationAssistRequest(resume_id="fixture")
    calls=[]

    async def model(snapshot):
        calls.append(snapshot)
        return FormPlan(actions=[FillAction(selector="#intro",label="介绍你的适合之处",action="fill",
            value="合成已证据约束的模型答案",value_source="fixture evidence",confidence=.99,resolution_source="model"),
            # Even a confirmed-looking model action cannot tick declarations.
            FillAction(selector="#agree",label="承诺",action="check",value=True,confidence=1,user_confirmed=True)])

    async def prepare(browser,*,planner=model,guard=lambda:None):
        return await assist.prepare_application(browser,"fixture",request,profile,
            guard=guard,model_plan=planner,stamp_plan=lambda plan:plan)

    def local(snapshot,candidate):
        return route_local_plan(_local_safe_plan(snapshot,candidate),snapshot,candidate)

    with patch.object(assist,"create_local_form_plan",side_effect=local):
        browser=UnclearBrowser()
        result=await prepare(browser)
        assert result.status=="ready_for_review",result
        assert len(calls)==1 and len(browser.calls)==1
        assert len(browser.calls[0].actions)==1 and browser.calls[0].actions[0].selector=="#intro"
        assert len(result.pre_submit.required_missing)==1

        # A visible value can match even when committing the widget failed
        # (observed with an owned calendar panel). Final review must not erase
        # that failure or announce completion just from the input string.
        browser=Browser()
        browser.values.update(country="中国",city="北京")
        browser.options_loaded=True
        original_execute=browser.execute
        async def displayed_but_unverified(sid,payload,before_action=None,on_progress=None):
            execution=await original_execute(sid,payload,before_action=before_action,on_progress=on_progress)
            for item in execution.results:
                if item.selector=="#name":
                    item.status="failed"
                    item.verified=False
                    item.message="合成控件提交状态未通过核验"
            execution.verified=sum(item.verified for item in execution.results)
            execution.completed=execution.verified
            execution.failed=sum(item.status=="failed" for item in execution.results)
            return execution
        browser.execute=displayed_but_unverified
        result=await prepare(browser)
        name=next(item for item in result.review.comparisons if item.selector=="#name")
        assert browser.values["name"]==profile.name
        assert name.status=="manual_review" and "不计为填写成功" in name.recommendation
        assert result.status=="partial" and len(browser.calls)==1
        assert result.review.summary.matched==2 and result.review.summary.manual_review>=1
        # Same selector, different question: no stale failure association.
        field=next(f for f in result.review.snapshot.fields if f.selector=="#name")
        failure={field.selector:assist._field_identity(field)}
        changed=field.model_copy(update={"question_text":"另一道题"})
        unchanged=result.review.model_copy(deep=True)
        unchanged.snapshot.fields=[changed if f.selector=="#name" else f for f in unchanged.snapshot.fields]
        name2=next(item for item in unchanged.comparisons if item.selector=="#name")
        name2.status="matched"
        unchanged.summary.matched+=1
        unchanged.summary.manual_review-=1
        assist._retain_execution_failures(unchanged,failure)
        assert name2.status=="matched"

        for exception in (asyncio.TimeoutError(),RuntimeError("private gateway details")):
            async def unavailable(snapshot):
                raise exception
            browser=UnclearBrowser()
            browser.values["name"]=""  # known rule fill happens before model failure
            result=await prepare(browser,planner=unavailable)
            assert result.status=="partial" and browser.values["name"]==profile.name
            assert len(browser.calls)==1 and browser.values["intro"]==""
            assert any(e.kind=="model_unavailable" for e in result.events)
            assert "private gateway" not in result.model_dump_json()

        browser=UnclearBrowser()
        async def while_editing(snapshot):
            browser.values["intro"]="用户正在编辑的真实答案"
            return await model(snapshot)
        result=await prepare(browser,planner=while_editing)
        assert result.status=="partial" and not browser.calls
        assert browser.values["intro"]=="用户正在编辑的真实答案"
        assert any("旧建议已丢弃" in e.message for e in result.events)

        browser=UnclearBrowser()
        browser.values["name"]=""
        def changed_context():
            if browser.values["name"] == profile.name:
                raise ValueError("合成资料版本已变，旧计划必须停止")
        result=await prepare(browser,guard=changed_context)
        assert result.status=="blocked" and len(browser.calls)==1
        assert browser.values["name"]==profile.name and browser.values["intro"]==""

        browser=UnclearBrowser()
        browser.values["name"]=""
        async def interrupted_batch(sid, payload, before_action=None, on_progress=None):
            browser.calls.append(payload)
            if on_progress:
                on_progress(1, 'read_value')
                on_progress(2, 'open_popup')
            browser.values["name"]=profile.name
            raise ExecutionTargetChanged("合成网页文档已重新加载", results=[ActionResult(
                selector="#name", label="姓名", status="filled", verified=True,
                actual_value="private-value", message="private-error")], current_label="下一题", current_phase="open_popup")
        browser.execute=interrupted_batch
        result=await prepare(browser)
        assert result.status=="blocked" and result.rounds==0 and result.pre_submit is None
        assert len(browser.calls)==1 and browser.values["intro"]==""
        interrupted=next(e for e in result.events if e.kind=="write_interrupted")
        assert interrupted.completed==interrupted.failed==0
        assert "1项" in interrupted.message and "整批最终核对未完成" in interrupted.message
        assert [item.label for item in interrupted.issues]==["姓名", "下一题"]
        assert execution_phase_label('open_popup') in interrupted.issues[-1].message
        assert all(e.completed == e.failed == 0 for e in result.events if e.kind == 'control')
        assert "private-value" not in interrupted.model_dump_json()
        assert "private-error" not in interrupted.model_dump_json()

        browser=Browser()
        browser.values.update(name=profile.name,country="中国",city="北京")
        browser.options_loaded=True
        original=browser.pre_submit_check
        async def changing_check(sid):
            check=await original(sid)
            browser.values["name"]="核对期间发生改动"
            return check
        browser.pre_submit_check=changing_check
        result=await prepare(browser)
        assert result.status=="partial" and result.review is None and result.pre_submit is None
        assert not browser.calls
        # A per-run skip must be bound to the exact current question, not just
        # its reusable selector. It must neither call the model nor write it.
        browser=UnclearBrowser()
        current=await browser.settled_snapshot('fixture')
        intro=next(f for f in current.fields if f.selector=='#intro')
        deferred_request=ApplicationAssistRequest(resume_id='fixture',deferred_fields=[intro])
        before=len(calls)
        result=await assist.prepare_application(browser,'fixture',deferred_request,profile,
            guard=lambda:None,model_plan=model,stamp_plan=lambda p:p)
        assert result.status=='needs_user' and not browser.calls and len(calls)==before
        assert any(f.label=='介绍你的适合之处' for f in result.pre_submit.required_missing)
        assert not result.pre_submit.ready
        wrong=intro.model_copy(update={'question_text':'另一道同 selector 的问题'})
        result=await assist.prepare_application(browser,'fixture',
            ApplicationAssistRequest(resume_id='fixture',deferred_fields=[wrong]),profile,
            guard=lambda:None,model_plan=model,stamp_plan=lambda p:p)
        assert result.status=='blocked' and not browser.calls and len(calls)==before
        # Deferrals may never masquerade as declaration consent.
        agree=next(f for f in current.fields if f.selector=='#agree')
        result=await assist.prepare_application(browser,'fixture',
            ApplicationAssistRequest(resume_id='fixture',deferred_fields=[agree]),profile,
            guard=lambda:None,model_plan=model,stamp_plan=lambda p:p)
        assert result.status=='blocked' and not browser.calls
    print("application_assist_safety_test: OK (model once, timeout, no guessed fallback, no consent, stale-page/context/final-check guards)")


if __name__=="__main__":
    asyncio.run(run())
