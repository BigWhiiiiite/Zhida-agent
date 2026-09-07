from __future__ import annotations

import json
import os
import re

from agents import Agent, Runner
from agents.exceptions import ModelBehaviorError
from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from .browser_models import BrowserSnapshot, FillAction, FormPlan, PageField
from .model_provider import configured_model
from .models import CandidateProfile


SYSTEM_PROMPT = """
你是职达的招聘表单映射 Agent。你只生成填写计划，不操作浏览器。

规则：
1. 每个 action.selector 必须原样复制页面字段提供的 selector。
2. 只使用候选人主档案中明确存在的信息，不猜测，不编造。
3. 姓名、邮箱、电话、城市、网站、教育和项目等明确字段可 fill/select/check。
4. 工作许可、签证担保、薪资、性别、族裔、残障、退伍军人、法律声明、同意条款等字段均 sensitive=true，action=ask_user，除非主档案存在完全明确且直接对应的答案。
5. 文件上传、验证码、密码、登录、最终提交按钮一律 skip。
6. select 的 value 必须是页面 options 中真实存在的完整文本；不能可靠匹配则 ask_user。
7. checkbox 只在含义明确且不是声明/同意/隐私确认时 check。
8. 找不到资料时 ask_user，并写明缺失问题。
9. 置信度低于 0.85 时不要自动填写。
10. 不得输出页面未提供的 selector。
"""


RETRYABLE_MODEL_ERRORS = (
    APIConnectionError, APITimeoutError, InternalServerError, RateLimitError, ModelBehaviorError,
)
MODEL_PLAN_ERRORS = RETRYABLE_MODEL_ERRORS + (RuntimeError, ValueError)

SENSITIVE_HINTS = (
    "authorization", "visa", "sponsorship", "salary", "compensation", "gender", "sex", "race",
    "ethnicity", "disability", "veteran", "consent", "agree", "privacy", "terms", "legal",
    "工作许可", "签证", "担保", "薪资", "性别", "种族", "族裔", "残障", "退伍", "同意", "隐私", "条款",
)


def _form_prompt(snapshot: BrowserSnapshot, profile: CandidateProfile) -> str:
    return json.dumps({
        "page": snapshot.model_dump(mode="json"),
        "candidate_profile": profile.model_dump(
            mode="json", exclude={"created_at", "updated_at"}, exclude_defaults=True, exclude_none=True,
        ),
    }, ensure_ascii=False)


def _form_plan_from_model_text(output: str) -> FormPlan:
    candidate = output.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```(?:json)?\s*", "", candidate, count=1, flags=re.I)
        candidate = re.sub(r"\s*```$", "", candidate, count=1)
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end < start:
        raise RuntimeError("备用模型没有返回有效的表单计划 JSON")
    return FormPlan.model_validate_json(candidate[start:end + 1])


def _keep_page_actions(plan: FormPlan, snapshot: BrowserSnapshot) -> FormPlan:
    allowed = {field.selector for field in snapshot.fields}
    plan.actions = [action for action in plan.actions if action.selector in allowed]
    return plan


def _direct_profile_value(field: PageField, profile: CandidateProfile) -> tuple[str, str]:
    label = f"{field.label} {field.name}".lower()
    education = next((item for item in profile.education if item.current), None)
    education = education or (profile.education[0] if profile.education else None)
    current_job = next((item for item in profile.internships if item.current), None)
    current_job = current_job or (profile.internships[0] if profile.internships else None)
    mappings = (
        (("email", "e-mail", "邮箱", "电子邮件"), profile.email, "主档案.email"),
        (("phone", "mobile", "telephone", "手机", "电话"), profile.phone, "主档案.phone"),
        (("linkedin",), profile.linkedin, "主档案.linkedin"),
        (("github",), profile.github, "主档案.github"),
        (("portfolio", "personal website", "个人网站", "作品集"), profile.website, "主档案.website"),
        (("current location", "location", "city", "当前所在地", "居住地", "城市"), profile.location,
         "主档案.location"),
        (("current company", "current employer", "当前公司", "当前雇主"),
         current_job.organization if current_job else "", "主档案.internships"),
        (("school", "university", "学校", "院校"), education.school if education else "", "主档案.education.school"),
        (("college", "faculty", "department", "学院", "院系"), education.college if education else "",
         "主档案.education.college"),
        (("major", "field of study", "专业"), education.major if education else "", "主档案.education.major"),
        (("degree", "学历", "学位"), education.degree if education else "", "主档案.education.degree"),
        (("graduat", "expected month", "毕业时间", "毕业日期", "毕业年月"),
         education.end_date if education else "", "主档案.education.end_date"),
        (("gpa", "绩点"), education.gpa if education else "", "主档案.education.gpa"),
        (("wechat", "weixin", "微信"), profile.wechat, "主档案.wechat"),
        (("full name", "legal name", "candidate name", "姓名", "名字"), profile.name, "主档案.name"),
    )
    for hints, value, source in mappings:
        if value and any(hint in label for hint in hints):
            return str(value), source
    return "", ""


def _matching_option(value: str, options: list[str]) -> str:
    wanted = value.casefold().strip()
    for option in options:
        if option.casefold().strip() == wanted:
            return option
    for option in options:
        normalized = option.casefold().strip()
        if wanted and (wanted in normalized or normalized in wanted):
            return option
    return ""


def _local_safe_plan(snapshot: BrowserSnapshot, profile: CandidateProfile) -> FormPlan:
    actions: list[FillAction] = []
    missing: list[str] = []
    cities = [*profile.target_cities, profile.location]
    for field in snapshot.fields:
        label = f"{field.label} {field.name}".lower()
        sensitive = any(hint in label for hint in SENSITIVE_HINTS)
        if field.field_type == "file":
            action = FillAction(selector=field.selector, label=field.label or field.name, action="skip",
                                reason="文件由前端的简历选择器单独上传", confidence=1)
        elif sensitive:
            action = FillAction(selector=field.selector, label=field.label or field.name, action="ask_user",
                                reason="敏感或同意类字段需要用户确认", sensitive=True, confidence=1)
        elif field.field_type in {"checkbox", "radio"}:
            city = next((city for city in cities if city and city.casefold() in label), "")
            if city:
                action = FillAction(selector=field.selector, label=field.label or field.name, action="check",
                                    value=True, value_source="主档案.target_cities/location", confidence=.95)
            else:
                kind = "ask_user" if field.required else "skip"
                action = FillAction(selector=field.selector, label=field.label or field.name, action=kind,
                                    reason="单选或复选含义无法从主档案确定", confidence=1)
        else:
            value, source = _direct_profile_value(field, profile)
            if value and field.field_type in {"select-one", "select-multiple"}:
                value = _matching_option(value, field.options)
            if value:
                action = FillAction(selector=field.selector, label=field.label or field.name,
                                    action="select" if field.field_type.startswith("select") else "fill",
                                    value=value, value_source=source, confidence=.99)
            else:
                kind = "ask_user" if field.required else "skip"
                action = FillAction(selector=field.selector, label=field.label or field.name, action=kind,
                                    reason="主档案中没有可直接确认的值", confidence=1)
        actions.append(action)
        if field.required and action.action == "ask_user":
            missing.append(field.label or field.name or field.field_type)
    site_type = next((site for site in ("lever", "greenhouse", "workday") if site in snapshot.url.lower()), "generic")
    return FormPlan(page_summary="本地安全映射已生成；不明确的字段已保留给用户确认。",
                    site_type=site_type, actions=actions, missing_questions=missing)


def _merge_plans(base: FormPlan, model_plan: FormPlan, snapshot: BrowserSnapshot) -> FormPlan:
    replaceable = {action.selector for action in base.actions if action.action in {"ask_user", "skip"}}
    replacements = {action.selector: action for action in model_plan.actions if action.selector in replaceable}
    base.actions = [replacements.get(action.selector, action) for action in base.actions]
    required = {field.selector: field for field in snapshot.fields if field.required}
    base.missing_questions = [
        required[action.selector].label or required[action.selector].name
        for action in base.actions if action.selector in required and action.action == "ask_user"
    ]
    if model_plan.page_summary:
        base.page_summary = model_plan.page_summary
    if model_plan.site_type and model_plan.site_type != "generic":
        base.site_type = model_plan.site_type
    return base


async def _structured_plan(snapshot: BrowserSnapshot, profile: CandidateProfile,
                           model_name: str, timeout: float) -> FormPlan:
    model, settings = configured_model(model_name, "low", timeout)
    agent = Agent(name="Zhida Form Mapper", instructions=SYSTEM_PROMPT, model=model,
                  model_settings=settings, output_type=FormPlan)
    result = await Runner.run(agent, _form_prompt(snapshot, profile), max_turns=1)
    if not isinstance(result.final_output, FormPlan):
        raise RuntimeError("模型没有返回有效的表单填写计划")
    return _keep_page_actions(result.final_output, snapshot)


async def _prompt_json_plan(snapshot: BrowserSnapshot, profile: CandidateProfile,
                            model_name: str, timeout: float) -> FormPlan:
    model, settings = configured_model(model_name, "low", timeout)
    schema = json.dumps(FormPlan.model_json_schema(), ensure_ascii=False)
    agent = Agent(
        name="Zhida Form Mapper Fallback",
        model=model,
        model_settings=settings,
        instructions=(SYSTEM_PROMPT + "\n只输出一个符合用户消息中 JSON Schema 的 JSON 对象，"
                      "不要 Markdown、代码围栏或解释。"),
    )
    prompt = f"JSON Schema:\n{schema}\n\n请生成表单填写计划：\n{_form_prompt(snapshot, profile)}"
    result = await Runner.run(agent, prompt, max_turns=1)
    if not isinstance(result.final_output, str):
        raise RuntimeError("备用模型没有返回 JSON 文本")
    return _keep_page_actions(_form_plan_from_model_text(result.final_output), snapshot)


async def create_form_plan(snapshot: BrowserSnapshot, profile: CandidateProfile) -> FormPlan:
    # Load backend/.env before reading the routing variables below.
    configured_model()
    local_plan = _local_safe_plan(snapshot, profile)
    unresolved = {action.selector for action in local_plan.actions if action.action in {"ask_user", "skip"}}
    model_fields = [field for field in snapshot.fields if field.selector in unresolved and field.field_type != "file"]
    if not model_fields:
        return local_plan
    model_snapshot = snapshot.model_copy(update={"fields": model_fields})
    primary_model = os.getenv("APP_AGENT_MODEL", "gpt-5.6-sol").strip()
    fallback_model = os.getenv("APP_AGENT_FALLBACK_MODEL", "").strip()
    prompt_json_models = {
        item.strip() for item in os.getenv("APP_AGENT_PROMPT_JSON_MODELS", "").split(",") if item.strip()
    }
    primary_timeout = float(os.getenv("APP_FORM_MODEL_TIMEOUT_SECONDS", "30"))
    fallback_timeout = float(os.getenv("APP_FORM_FALLBACK_TIMEOUT_SECONDS", "45"))
    try:
        runner = _prompt_json_plan if primary_model in prompt_json_models else _structured_plan
        model_plan = await runner(model_snapshot, profile, primary_model, primary_timeout)
        return _merge_plans(local_plan, model_plan, snapshot)
    except MODEL_PLAN_ERRORS as primary_error:
        if not fallback_model or fallback_model == primary_model:
            local_plan.page_summary = f"主模型暂时不可用，已使用本地安全映射：{primary_error}"
            return local_plan
        try:
            runner = _prompt_json_plan if fallback_model in prompt_json_models else _structured_plan
            model_plan = await runner(model_snapshot, profile, fallback_model, fallback_timeout)
            return _merge_plans(local_plan, model_plan, snapshot)
        except Exception as fallback_error:
            local_plan.page_summary = (
                f"主模型 {primary_model} 和备用模型 {fallback_model} 暂时不可用，"
                f"已使用本地安全映射：{fallback_error}"
            )
            return local_plan
