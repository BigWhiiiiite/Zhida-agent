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
11. 必须识别字段的信息主体。紧急联系人、家属、监护人、推荐人等第三方信息不得使用候选人本人的姓名、电话或邮箱。
12. 是/否、是否接受调剂、意向事业群、工作偏好等需要候选人决策的字段，没有已确认答案时必须 ask_user。
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

THIRD_PARTY_HINTS = (
    "emergency contact", "emergency phone", "emergency mobile", "next of kin", "guardian",
    "reference name", "reference phone", "referee", "recommender", "family contact",
    "紧急联系人", "紧急联络人", "紧急联系方式", "紧急联络方式", "家属联系人", "家庭联系人",
    "监护人", "推荐人", "证明人", "介绍人", "联系人姓名", "联系人电话",
)

MANUAL_DECISION_HINTS = (
    "是否", "愿意", "接受调剂", "服从调剂", "服从分配", "意向事业群", "感兴趣的事业群",
    "岗位志愿", "地点志愿", "工作偏好", "可否", "would you", "are you willing",
    "willing to", "preference", "preferred business", "business group", "relocate",
)

YES_NO_OPTIONS = {"是", "否", "yes", "no", "y", "n", "true", "false"}

DEGREE_ALIASES = {
    "high_school": ("高中", "中专", "high school", "secondary school"),
    "associate": ("大专", "专科", "associate", "college diploma"),
    "bachelor": ("本科", "学士", "bachelor", "undergraduate", "bsc", "bs", "beng"),
    "master": ("硕士", "研究生", "master", "msc", "ms", "meng"),
    "doctorate": ("博士", "doctor", "phd", "doctoral"),
}
DEGREE_RANK = {"high_school": 0, "associate": 1, "bachelor": 2, "master": 3, "doctorate": 4}


def _normalized(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def _field_text(field: PageField) -> str:
    return " ".join(filter(None, (field.section, field.group_label, field.label, field.name))).casefold()


def _is_third_party(field: PageField) -> bool:
    text = _field_text(field)
    return any(hint in text for hint in THIRD_PARTY_HINTS)


def _saved_answer(field: PageField, profile: CandidateProfile) -> str:
    targets = {_normalized(value) for value in (field.group_label, field.label) if value}
    for question, value in profile.application_answers.items():
        if value and _normalized(question) in targets:
            return value
    return ""


def _has_yes_no_options(field: PageField) -> bool:
    options = {_normalized(option) for option in field.options}
    normalized_yes_no = {_normalized(option) for option in YES_NO_OPTIONS}
    return bool(options) and options.issubset(normalized_yes_no) and len(options) >= 2


def _needs_manual_decision(field: PageField) -> bool:
    text = _field_text(field)
    return _has_yes_no_options(field) or any(hint in text for hint in MANUAL_DECISION_HINTS)


def _degree_family(value: str) -> str:
    normalized = value.casefold()
    for family, aliases in DEGREE_ALIASES.items():
        if any(alias in normalized for alias in aliases):
            return family
    return ""


def _education_for_field(field: PageField, profile: CandidateProfile):
    if not profile.education:
        return None
    text = _field_text(field)
    if "最高" in text or "highest" in text:
        return max(profile.education, key=lambda item: DEGREE_RANK.get(_degree_family(item.degree), -1))
    return next((item for item in profile.education if item.current), None) or profile.education[0]


def _matching_degree_option(value: str, options: list[str], label: str) -> str:
    exact = _matching_option(value, options)
    if exact:
        return exact
    family = _degree_family(value)
    candidates = [option for option in options if _degree_family(option) == family] if family else []
    if len(candidates) <= 1:
        return candidates[0] if candidates else ""
    wants_degree = "学位" in label or "degree awarded" in label.casefold()
    if wants_degree:
        degree_word = {"bachelor": "学士", "master": "硕士", "doctorate": "博士"}.get(family, "")
        preferred = next((item for item in candidates if degree_word and degree_word in item), None)
        if preferred:
            return preferred
    wants_level = "学历" in label or "education level" in label.casefold()
    if wants_level:
        level_word = {"associate": "专科", "bachelor": "本科", "master": "研究生", "doctorate": "博士"}.get(family, "")
        preferred = next((item for item in candidates if level_word and level_word in item), None)
        if preferred:
            return preferred
    return ""


def _manual_answer_action(field: PageField, answer: str) -> FillAction:
    label = field.label or field.group_label or field.name
    if field.field_type == "radio":
        option = field.option_label or field.option_value
        matches = _normalized(answer) in {_normalized(option), _normalized(field.option_value)}
        return FillAction(selector=field.selector, label=label, action="check" if matches else "skip",
                          value=True if matches else "", value_source="主档案.application_answers",
                          confidence=1, user_confirmed=True, reason="使用用户已确认的同题答案")
    if field.field_type == "checkbox":
        checked = _normalized(answer) in {_normalized(value) for value in ("是", "yes", "true", "1")}
        return FillAction(selector=field.selector, label=label, action="check", value=checked,
                          value_source="主档案.application_answers", confidence=1, user_confirmed=True,
                          reason="使用用户已确认的同题答案")
    if field.field_type in {"select-one", "select-multiple", "combobox"}:
        selected = _matching_option(answer, field.options) if field.options else ""
        if not selected:
            return FillAction(selector=field.selector, label=label, action="ask_user", sensitive=False,
                              confidence=1, reason="已保存答案与当前网页选项不一致，需要重新确认")
        return FillAction(selector=field.selector, label=label, action="select", value=selected,
                          value_source="主档案.application_answers", confidence=1, user_confirmed=True)
    return FillAction(selector=field.selector, label=label, action="fill", value=answer,
                      value_source="主档案.application_answers", confidence=1, user_confirmed=True)


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
    if _is_third_party(field):
        return "", ""
    label = _field_text(field)
    education = _education_for_field(field, profile)
    current_job = next((item for item in profile.internships if item.current), None)
    current_job = current_job or (profile.internships[0] if profile.internships else None)
    if education and any(hint in label for hint in ("degree", "education level", "学历", "学位")):
        degree = (_matching_degree_option(education.degree, field.options, label)
                  if field.options else education.degree)
        if degree:
            return degree, "主档案.education.degree"
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
        (("graduat", "expected month", "毕业时间", "毕业日期", "毕业年月"),
         education.end_date if education else "", "主档案.education.end_date"),
        (("gpa", "绩点"), education.gpa if education else "", "主档案.education.gpa"),
        (("qq", "qq号", "qq号码", "qq account"), profile.qq, "主档案.qq"),
        (("wechat", "weixin", "微信"), profile.wechat, "主档案.wechat"),
        (("full name", "legal name", "candidate name", "姓名", "名字"), profile.name, "主档案.name"),
    )
    for hints, value, source in mappings:
        if value and any(hint in label for hint in hints):
            return str(value), source
    saved = _saved_answer(field, profile)
    if saved:
        return saved, "主档案.application_answers"
    return "", ""


def _matching_option(value: str, options: list[str]) -> str:
    wanted = _normalized(value)
    for option in options:
        if _normalized(option) == wanted:
            return option
    partial = [option for option in options
               if wanted and (wanted in _normalized(option) or _normalized(option) in wanted)]
    return partial[0] if len(partial) == 1 else ""


def _local_safe_plan(snapshot: BrowserSnapshot, profile: CandidateProfile) -> FormPlan:
    actions: list[FillAction] = []
    missing: list[str] = []
    cities = [*profile.target_cities, profile.location]
    for field in snapshot.fields:
        label = _field_text(field)
        sensitive = any(hint in label for hint in SENSITIVE_HINTS)
        saved_answer = _saved_answer(field, profile)
        if field.field_type == "file":
            action = FillAction(selector=field.selector, label=field.label or field.name, action="skip",
                                reason="文件由前端的简历选择器单独上传", confidence=1)
        elif _is_third_party(field):
            action = FillAction(selector=field.selector, label=field.label or field.group_label or field.name,
                                action="ask_user", reason="第三方联系信息不得使用候选人本人资料",
                                sensitive=True, confidence=1)
        elif sensitive:
            action = FillAction(selector=field.selector, label=field.label or field.name, action="ask_user",
                                reason="敏感或同意类字段需要用户确认", sensitive=True, confidence=1)
        elif _needs_manual_decision(field):
            action = (_manual_answer_action(field, saved_answer) if saved_answer else
                      FillAction(selector=field.selector, label=field.label or field.group_label or field.name,
                                 action="ask_user", reason="是否/偏好类问题需要用户明确选择",
                                 confidence=1))
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
            if value and field.field_type in {"select-one", "select-multiple", "combobox"}:
                value = _matching_option(value, field.options)
            if value:
                action = FillAction(selector=field.selector, label=field.label or field.name,
                                    action="select" if field.field_type.startswith("select") or field.field_type == "combobox" else "fill",
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


def _enforce_policy(plan: FormPlan, snapshot: BrowserSnapshot, profile: CandidateProfile) -> FormPlan:
    """Apply deterministic safety rules after the model so it cannot bypass them."""
    fields = {field.selector: field for field in snapshot.fields}
    guarded: list[FillAction] = []
    for action in plan.actions:
        field = fields.get(action.selector)
        if not field:
            continue
        label = field.label or field.group_label or field.name
        text = _field_text(field)
        if _is_third_party(field):
            guarded.append(FillAction(selector=field.selector, label=label, action="ask_user", value="",
                                      confidence=1, sensitive=True,
                                      reason="第三方联系信息必须由用户提供，禁止使用候选人资料"))
            continue
        if any(hint in text for hint in SENSITIVE_HINTS):
            guarded.append(FillAction(selector=field.selector, label=label, action="ask_user", value="",
                                      confidence=1, sensitive=True,
                                      reason="敏感或声明类字段必须由用户确认"))
            continue
        if _needs_manual_decision(field):
            saved = _saved_answer(field, profile)
            guarded.append(_manual_answer_action(field, saved) if saved else
                           FillAction(selector=field.selector, label=label, action="ask_user", value="",
                                      confidence=1, reason="是否/偏好类问题需要用户明确选择"))
            continue
        if action.action == "select" and field.options:
            selected = _matching_option(str(action.value), field.options)
            if not selected:
                guarded.append(FillAction(selector=field.selector, label=label, action="ask_user", value="",
                                          confidence=1, reason="建议值与网页真实选项不一致"))
                continue
            action.value = selected
        guarded.append(action)
    plan.actions = guarded
    required = {field.selector: field for field in snapshot.fields if field.required}
    plan.missing_questions = list(dict.fromkeys(
        (required[action.selector].group_label or required[action.selector].label or
         required[action.selector].name or required[action.selector].field_type)
        for action in plan.actions if action.selector in required and action.action == "ask_user"
    ))
    return plan


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
    # Large ATS pages can produce several thousand output tokens. Relay-backed
    # Responses calls may finish successfully after a minute, so keep form
    # deadlines independent from the lightweight health-check deadline.
    primary_timeout = float(os.getenv("APP_FORM_MODEL_TIMEOUT_SECONDS", "180"))
    fallback_timeout = float(os.getenv("APP_FORM_FALLBACK_TIMEOUT_SECONDS", "120"))
    try:
        runner = _prompt_json_plan if primary_model in prompt_json_models else _structured_plan
        model_plan = await runner(model_snapshot, profile, primary_model, primary_timeout)
        return _enforce_policy(_merge_plans(local_plan, model_plan, snapshot), snapshot, profile)
    except MODEL_PLAN_ERRORS as primary_error:
        if not fallback_model or fallback_model == primary_model:
            local_plan.page_summary = f"主模型暂时不可用，已使用本地安全映射：{primary_error}"
            return local_plan
        try:
            runner = _prompt_json_plan if fallback_model in prompt_json_models else _structured_plan
            model_plan = await runner(model_snapshot, profile, fallback_model, fallback_timeout)
            return _enforce_policy(_merge_plans(local_plan, model_plan, snapshot), snapshot, profile)
        except Exception as fallback_error:
            local_plan.page_summary = (
                f"主模型 {primary_model} 和备用模型 {fallback_model} 暂时不可用，"
                f"已使用本地安全映射：{fallback_error}"
            )
            return local_plan
