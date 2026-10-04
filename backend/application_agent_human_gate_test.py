"""Offline regression: a human-only remainder is not an autofill loop.

No API startup, dotenv loading, database access, browser or model is required.
All candidate data and page controls below are synthetic fixtures.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.application_agent import _allowed_actions, decide_application_step
from app.application_models import ApplicationWorkflowState
from app.browser_models import BrowserSnapshot, PageField, PreSubmitCheck, RequiredFieldIssue
from app.models import CandidateProfile


URL = "https://jobs.example.test/apply"
PROFILE = CandidateProfile(name="合成测试姓名")


def snapshot(fields: list[PageField]) -> BrowserSnapshot:
    return BrowserSnapshot(session_id="human-gate-test", url=URL, title="合成申请表", fields=fields)


def workflow(stage: str, count: int, **updates) -> ApplicationWorkflowState:
    return ApplicationWorkflowState(session_id="human-gate-test", url=URL, title="合成申请表",
        stage=stage, form_fields=count, final_submit_present=stage == "review", **updates)


def missing(field: PageField, **updates) -> PreSubmitCheck:
    return PreSubmitCheck(url=URL, ready=False, required_total=1, required_missing=[
        RequiredFieldIssue(selector=field.selector, label=field.label, field_type=field.field_type),
    ], **updates)


async def main() -> None:
    declaration = PageField(selector="#truth", label="我承诺以上资料真实并承担法律责任",
        label_source="label", field_type="checkbox", required=True, current_value="false")
    file = PageField(selector="#cv", label="简历附件", label_source="label", field_type="file", required=True)
    birth = PageField(selector="#dob", label="出生日期", label_source="label", field_type="date", required=True)
    matched = PageField(selector="#name", label="姓名", label_source="label", current_value=PROFILE.name)
    for remainder in (declaration, file, birth):
        for stage in ("review", "application_form", "profile_form"):
            page = snapshot([matched, remainder])
            state = workflow(stage, 2)
            check = missing(remainder)
            allowed = _allowed_actions(state, check, page, PROFILE)
            assert "analyze_and_fill" not in allowed, (remainder.label, stage, allowed)
            # Reassessment cannot spend model calls trying to overcome a human
            # boundary or let a stage-conflict narrative restore auto-execution.
            with patch("app.application_agent.configured_model") as model, \
                    patch("app.application_agent.Runner.run") as runner:
                for _ in range(3):
                    decision = await decide_application_step(state, page, PROFILE, check)
                    assert decision.next_action == ("review_before_submit" if stage == "review" else "stop")
                    assert not decision.can_execute and decision.requires_user
                    assert "没有可自动补写" in decision.summary
                    assert any(remainder.label in question for question in decision.user_questions)
                    assert "1 个必填项仍未填写" in decision.blockers
                model.assert_not_called()
                runner.assert_not_called()
            assert declaration.current_value == "false"  # decision has no mutation rights

    # The same false readiness flag must not hide actual safe work. Optional
    # known gaps still count, even when the site marks all required fields done.
    empty_name = matched.model_copy(update={"current_value": ""})
    for ready in (False, True):
        for stage in ("review", "application_form"):
            page = snapshot([empty_name, declaration])
            decision = await decide_application_step(workflow(stage, 2), page, PROFILE,
                PreSubmitCheck(url=URL, ready=ready), use_model=False)
            assert decision.next_action == "analyze_and_fill" and decision.can_execute

    # A genuinely model-routed question is not mistaken for a permanent human
    # gate merely because the deterministic mapper has no immediate write.
    unclear = PageField(selector="#ambiguous", label="介绍你的适合之处", label_source="label",
                        field_type="textarea", required=True)
    page = snapshot([unclear])
    decision = await decide_application_step(workflow("review", 1), page, PROFILE,
                                            missing(unclear), use_model=False)
    assert decision.next_action == "analyze_and_fill" and decision.can_execute

    # A website validation problem without any known or model-routable gap is
    # surfaced to the user, not portrayed as final success or retried forever.
    page = snapshot([matched])
    state = workflow("review", 1)
    check = PreSubmitCheck(url=URL, ready=False, validation_errors=["请确认其他页面的必填资料"])
    decision = await decide_application_step(state, page, PROFILE, check, use_model=False)
    assert not decision.can_execute and "1 个网页校验错误" in decision.blockers
    assert "确认项或网页提示" in decision.summary

    # An empty/half-loaded form still needs fresh evidence; it is not complete.
    decision = await decide_application_step(workflow("review", 0), snapshot([]), PROFILE,
                                            PreSubmitCheck(url=URL, ready=True), use_model=False)
    assert decision.next_action == "refresh"

    # Human gates must not break normal verified multi-page continuation.
    state = workflow("application_form", 1, safe_next_present=True, safe_next_label="下一步")
    decision = await decide_application_step(state, snapshot([matched]), PROFILE,
                                            PreSubmitCheck(url=URL, ready=True), use_model=False)
    assert decision.next_action == "continue_application" and decision.can_execute
    state.requires_consent = True
    decision = await decide_application_step(state, snapshot([matched]), PROFILE,
                                            PreSubmitCheck(url=URL, ready=False), use_model=False)
    assert decision.next_action == "stop" and not decision.can_execute
    print("application_agent_human_gate_test: OK")


if __name__ == "__main__":
    asyncio.run(main())
