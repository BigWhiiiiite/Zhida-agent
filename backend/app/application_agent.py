"""Model-informed, code-governed orchestration for one application page at a time.

The model explains and prioritizes the next step across the whole application
journey. The state machine remains authoritative and exposes only bounded safe
actions; credentials, verification, consent, and final submission are never
delegated to the model.
"""

from __future__ import annotations

import json
import os

from agents import Agent, Runner

from .application_models import (AgentNextAction, ApplicationAgentDecision,
                                 ApplicationWorkflowState)
from .browser_models import BrowserSnapshot, PreSubmitCheck
from .model_provider import configured_model
from .models import CandidateProfile
from .form_agent import build_form_review, create_local_form_plan


SAFE_EXECUTABLE_ACTIONS = {
    "start_application", "analyze_and_fill", "continue_application", "refresh",
}


def _allowed_actions(workflow: ApplicationWorkflowState,
                     check: PreSubmitCheck | None,
                     snapshot: BrowserSnapshot | None = None,
                     profile: CandidateProfile | None = None) -> list[AgentNextAction]:
    if workflow.stage == "job_detail":
        return ["start_application", "stop"]
    if workflow.stage == "registration_required":
        return ["wait_for_registration", "refresh"]
    if workflow.stage == "auth_required":
        return ["wait_for_login", "refresh"]
    if workflow.stage == "verification_required":
        return ["wait_for_verification", "refresh"]
    if check and check.human_challenges:
        return ["wait_for_verification", "refresh"]
    known_gaps = False
    if snapshot and profile and workflow.stage in {"profile_form", "application_form", "review"}:
        plan = create_local_form_plan(snapshot, profile)
        automatic = {action.selector for action in plan.actions
                     if action.action in {"fill", "select", "check"} and action.confidence >= .85
                     and (not action.sensitive or action.user_confirmed)}
        review = build_form_review(snapshot, plan)
        known_gaps = any(item.selector in automatic and item.status in {"missing", "conflict"}
                         for item in review.comparisons)
    if workflow.stage == "review" or workflow.final_submit_present:
        # Many ATSs show the final button throughout an editable one-page form.
        # That button forbids submission, not safe filling of the remaining fields.
        if known_gaps or (check and not check.ready and (workflow.form_fields or (snapshot and snapshot.fields))):
            return ["analyze_and_fill", "review_before_submit", "refresh"]
        return ["review_before_submit", "refresh"]
    if workflow.stage in {"profile_form", "application_form"}:
        if check and check.ready and not known_gaps and workflow.safe_next_present and not workflow.requires_consent:
            return ["continue_application", "review_before_submit"]
        return ["analyze_and_fill", "refresh"]
    return ["refresh", "stop"]


def _local_decision(workflow: ApplicationWorkflowState,
                    snapshot: BrowserSnapshot,
                    check: PreSubmitCheck | None,
                    profile: CandidateProfile | None = None) -> ApplicationAgentDecision:
    allowed = _allowed_actions(workflow, check, snapshot, profile)
    action = allowed[0]
    mapping: dict[AgentNextAction, tuple[str, str, str, bool, str]] = {
        "start_application": (
            "进入正式申请流程", "打开申请入口并重新判断登录或注册状态",
            "当前是岗位详情页，可以执行不涉及提交的入口跳转。", False, "low",
        ),
        "wait_for_registration": (
            "完成账号注册", "等待你核对注册资料、协议和账号创建",
            "创建账号、设置密码及同意隐私条款需要你的明确操作。", True, "high",
        ),
        "wait_for_login": (
            "恢复招聘网站登录状态", "请在专用浏览器中完成登录，然后重新识别",
            "系统不会读取或保存招聘网站密码。", True, "high",
        ),
        "wait_for_verification": (
            "完成人工身份验证", "等待你输入短信、邮件验证码或完成人机验证",
            "验证码和人机验证属于人工边界，不能由 Agent 绕过。", True, "high",
        ),
        "analyze_and_fill": (
            "完成当前页可安全填写的资料", "分析字段、填写高置信度值并回读检查",
            "模型理解疑难字段，确定性策略会再次拦截敏感、未知和低置信度操作。", False, "medium",
        ),
        "continue_application": (
            "进入下一页申请步骤", f"当前页检查通过，进入“{workflow.safe_next_label or '下一步'}”",
            "只有页面检查无必填缺失、校验错误和人工验证时才允许继续。", False, "medium",
        ),
        "review_before_submit": (
            "完成人工终审", "请对照网页、附件和岗位要求检查，最终提交由你点击",
            "系统已经到达最终提交边界，不会代替用户作出正式申请决定。", True, "high",
        ),
        "refresh": (
            "重新理解当前页面", "刷新网页快照并重新判断所处阶段",
            "当前页面结构或阶段不够明确，先重新观察，不执行填写或跳转。", False, "low",
        ),
        "stop": (
            "暂停本次投递", "等待用户决定是否继续",
            "当前没有可可靠执行的下一步。", True, "low",
        ),
    }
    goal, summary, rationale, requires_user, risk = mapping[action]
    blockers: list[str] = []
    questions: list[str] = []
    if check:
        if check.required_missing:
            blockers.append(f"{len(check.required_missing)} 个必填项仍未填写")
        if check.validation_errors:
            blockers.append(f"{len(check.validation_errors)} 个网页校验错误")
        if check.human_challenges:
            blockers.append(f"{len(check.human_challenges)} 个人工验证")
    if workflow.requires_consent:
        blockers.append("隐私政策或用户协议需要本人确认")
    if action == "wait_for_registration":
        questions.append("请确认注册邮箱/手机号、密码及网站协议后继续")
    elif action == "wait_for_login":
        questions.append("完成招聘网站登录后，请让 Agent 重新识别")
    elif action == "wait_for_verification":
        questions.append("请输入你收到的验证码或手动完成人机验证")
    elif action == "review_before_submit":
        questions.append("请确认所有信息真实无误，再由你点击最终提交")
    return ApplicationAgentDecision(
        stage=workflow.stage, goal=goal, summary=summary, next_action=action,
        next_label=summary, rationale=rationale, blockers=blockers,
        user_questions=questions, can_execute=action in SAFE_EXECUTABLE_ACTIONS,
        requires_user=requires_user, risk_level=risk, model_status="local_fallback",
    )


def _decision_context(workflow: ApplicationWorkflowState, snapshot: BrowserSnapshot,
                      profile: CandidateProfile,
                      check: PreSubmitCheck | None) -> str:
    # The orchestration model needs readiness and page structure, not the
    # candidate's raw PII or answers. Actual values stay in the form mapper.
    fields = [{
        "question": field.question_text or field.group_label or field.label,
        "type": field.field_type,
        "required": field.required,
        "filled": bool(field.current_value),
        "semantic_key": field.semantic_key,
        "entity_scope": field.entity_scope,
        "option_count": len(field.options),
    } for field in snapshot.fields[:100]]
    payload = {
        "authoritative_stage": workflow.stage,
        "allowed_next_actions": _allowed_actions(workflow, check, snapshot, profile),
        "workflow": workflow.model_dump(mode="json", exclude={"actions"}),
        "page": {"title": snapshot.title, "recognition_profile": snapshot.recognition_profile,
                 "site_route": snapshot.site_route.model_dump(mode="json"),
                 "fields": fields},
        "profile_readiness": {
            "has_name": bool(profile.name), "has_email": bool(profile.email),
            "has_phone": bool(profile.phone), "education_records": len(profile.education),
            "internship_records": len(profile.internships), "project_records": len(profile.projects),
            "skill_count": len(profile.skills), "has_default_preferences": bool(profile.target_cities),
        },
        "pre_submit_check": check.model_dump(mode="json") if check else None,
    }
    return json.dumps(payload, ensure_ascii=False)


async def decide_application_step(workflow: ApplicationWorkflowState,
                                  snapshot: BrowserSnapshot,
                                  profile: CandidateProfile,
                                  check: PreSubmitCheck | None = None, *, use_model: bool = True
                                  ) -> ApplicationAgentDecision:
    fallback = _local_decision(workflow, snapshot, check, profile)
    allowed = _allowed_actions(workflow, check, snapshot, profile)
    if not use_model:
        return fallback
    try:
        model, settings = configured_model(reasoning_effort="low", timeout_seconds=float(
            os.getenv("APP_WORKFLOW_MODEL_TIMEOUT_SECONDS", "60")
        ))
        agent = Agent(
            name="Zhida Application Orchestrator",
            model=model,
            model_settings=settings,
            output_type=ApplicationAgentDecision,
            instructions=(
                "你是求职投递流程规划 Agent。请理解当前网页阶段，解释目标、阻塞项和下一步。"
                "authoritative_stage 是代码识别的权威阶段，不得修改；next_action 必须严格取自 allowed_next_actions。"
                "你不操作浏览器，也不得建议绕过验证码、人机验证、隐私同意或最终提交。"
                "不要索取或复述姓名、邮箱、手机号、密码、验证码等值。"
                "表单阶段优先解释为什么可以自动填写、哪些内容仍需用户确认。"
                "site_route 是代码根据当前域名和页面证据选择的工具策略；只能使用其中工具，不得指定新网址、选择器或脚本。"
                "输出简洁中文；can_execute、requires_user、risk_level 和 model_status 将由代码重新判定。"
            ),
        )
        result = await Runner.run(agent, _decision_context(workflow, snapshot, profile, check), max_turns=1)
        if not isinstance(result.final_output, ApplicationAgentDecision):
            return fallback
        draft = result.final_output
        if draft.next_action not in allowed:
            # Reject the narrative as well as the action: do not display a
            # misleading 'auto-fill now' label for a blocked verification step.
            return fallback
        action = draft.next_action if draft.next_action in allowed else fallback.next_action
        requires_user = action in {
            "wait_for_registration", "wait_for_login", "wait_for_verification",
            "review_before_submit", "stop",
        }
        return draft.model_copy(update={
            "stage": workflow.stage,
            "next_action": action,
            "next_label": draft.next_label or fallback.next_label,
            "can_execute": action in SAFE_EXECUTABLE_ACTIONS,
            "requires_user": requires_user,
            "risk_level": ("high" if requires_user and action != "stop"
                           else "medium" if action in {"analyze_and_fill", "continue_application"}
                           else "low"),
            "model_status": "model",
            "model": os.getenv("APP_AGENT_MODEL", ""),
        })
    except Exception:
        return fallback
