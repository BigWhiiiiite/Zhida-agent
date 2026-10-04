"""Continue the product journey through bounded, audited safe steps.

This is orchestration of Zhida's own agent, not another browser driver. Login,
OTP, registration agreements, missing facts and final submission remain human
gates. Reinvocation observes the current page instead of replaying a checkpoint.
"""
from __future__ import annotations

from .application_assist import is_declaration, observation_key
from .application_models import ApplicationJourneyEvent, ApplicationJourneyResult


def stop_reason(turn):
    workflow, decision, assisted = turn.workflow, turn.decision, turn.assistance
    gates = {
        "auth_required": ("waiting_login", "请在招聘浏览器完成登录，再点击“让职达继续”；不会填写或保存你的密码"),
        "registration_required": ("waiting_registration", "请在招聘浏览器完成注册并自行确认协议，再让职达继续"),
        "verification_required": ("waiting_verification", "请本人完成验证码或人机验证，再让职达继续；系统不会绕过验证"),
    }
    if workflow.stage in gates:
        return gates[workflow.stage]
    if turn.pre_submit and turn.pre_submit.human_challenges:
        return gates["verification_required"]
    if workflow.navigation_blocker or decision.stage_conflict:
        return "blocked", workflow.navigation_blocker or decision.summary
    if assisted and assisted.status != "ready_for_review":
        return assisted.status, assisted.message
    if assisted and observation_key(assisted.snapshot) != observation_key(turn.snapshot):
        return "partial", "填写后的页面又发生变化，旧核对结果不再有效；请重新读取后继续"
    declarations = {field.selector for field in turn.snapshot.fields if is_declaration(field)}
    if turn.pre_submit and (turn.pre_submit.validation_errors or any(
            field.selector not in declarations for field in turn.pre_submit.required_missing)):
        if assisted or not decision.can_execute:
            return "needs_user", "仍有必填信息或网页校验问题，请处理当前列出的确认项；尚未完成申请"
    if turn.review and not decision.can_execute and any(
            item.status != "matched" and item.selector not in declarations for item in turn.review.comparisons):
        return "needs_user", "还有未核实或待补充的资料，请按网页原题确认；非必填项也不会被当作已完成"
    final = workflow.stage == "review" or workflow.final_submit_present
    if final and assisted and assisted.status == "ready_for_review":
        return "ready_for_review", "当前可识别资料已补齐并核对，已停在人工终审；声明及最终提交由你操作"
    if final and turn.pre_submit and turn.pre_submit.ready and not decision.can_execute:
        return "ready_for_review", "当前页面检查通过，请本人核对完整申请及附件；职达不会点击最终提交"
    if decision.requires_user or not decision.can_execute:
        return "needs_user", decision.summary
    return None


async def continue_journey(*, max_steps, run_step, guard):
    events = []
    seen = set()
    turn = None
    for _ in range(max_steps):
        guard()
        # This call performs Zhida's ordinary model-informed step, whose code
        # policy, fresh-page checks, grounded form mapper and executor govern it.
        turn = await run_step()
        guard()
        events.append(ApplicationJourneyEvent(action=turn.action_taken or "observe",
            stage=turn.workflow.stage, message=turn.checkpoint.events[-1].summary
            if turn.checkpoint.events else turn.decision.summary))
        reason = stop_reason(turn)
        if reason:
            status, message = reason
            return ApplicationJourneyResult(status=status, message=message, turn=turn,
                                             steps=len(events), events=events)
        key = (turn.workflow.stage, turn.workflow.page_step_current, observation_key(turn.snapshot))
        if key in seen or not turn.action_taken or turn.action_taken == "refresh":
            return ApplicationJourneyResult(status="partial",
                message="页面未出现新的进展，已停止重复操作；请查看当前原因后再继续", turn=turn,
                steps=len(events), events=events)
        seen.add(key)
    return ApplicationJourneyResult(status="partial",
        message="已完成本轮有限安全步骤，尚未提交；请查看当前页面后继续", turn=turn,
        steps=len(events), events=events)
