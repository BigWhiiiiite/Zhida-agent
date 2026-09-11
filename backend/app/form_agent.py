from __future__ import annotations

import json
import os
import re

from agents import Agent, Runner
from agents.exceptions import ModelBehaviorError
from openai import APIConnectionError, APITimeoutError, InternalServerError, RateLimitError

from .browser_models import (BrowserSnapshot, ComparisonSummary, FieldComparison, FillAction,
                             FormPlan, FormReviewResult, PageField)
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
    "岗位志愿", "地点志愿", "工作偏好", "面试城市", "面试地点", "参加面试", "可否",
    "would you", "are you willing", "interview city", "interview location",
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

COUNTRY_HINTS = ("country/region", "country or region", "country", "国家/地区", "国家或地区", "所在国家")
INTERVIEW_LOCATION_HINTS = ("interview city", "interview location", "面试城市", "面试地点", "参加面试")
STUDY_LOCATION_HINTS = (
    "study location", "school location", "school city", "campus location", "就读地", "就读地点",
    "就读城市", "学校所在地", "学校所在城市", "院校所在地", "院校所在城市",
)
PREFERRED_LOCATION_HINTS = (
    "preferred location", "preferred city", "work city", "work location", "期望工作城市",
    "期望城市", "意向城市", "工作城市", "工作地点志愿",
)
CURRENT_LOCATION_HINTS = (
    "current location", "current city", "current residence", "当前所在地", "当前所处地",
    "目前所在地", "现居地", "居住地", "所在城市",
)
SKILL_HINTS = (
    "ai application skill", "ai skills", "technical skills", "professional skills",
    "skills", "skill set", "ai应用技能", "ai技能", "人工智能技能", "专业技能", "技术技能", "技能特长",
)
LANGUAGE_HINTS = ("language ability", "language skills", "languages", "语言能力", "外语能力", "掌握语言")
OPTIONAL_REVIEW_HINTS = SKILL_HINTS + LANGUAGE_HINTS + (
    "certificate", "certification", "award", "qualification", "证书", "奖项", "资质",
)
PLACEHOLDER_OPTIONS = {
    "", "select", "selectone", "choose", "chooseone", "pleasechoose", "请选择", "请选择一项",
    "暂未选择", "未选择", "点击选择", "搜索并选择",
}
OPTION_ALIASES = (
    {"男", "male", "man"}, {"女", "female", "woman"},
    {"中国", "中国大陆", "中华人民共和国", "china", "mainlandchina", "chn"},
    {"远程", "线上", "远程面试", "线上面试", "remote", "online"},
    {"英语", "英文", "english"}, {"普通话", "中文", "汉语", "mandarin", "chinese"},
)


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
    if _is_any(label, INTERVIEW_LOCATION_HINTS):
        saved = _saved_answer(field, profile)
        return (saved, "主档案.application_answers") if saved else ("", "")
    if _is_any(label, COUNTRY_HINTS):
        return (profile.country_region, "主档案.country_region") if profile.country_region else ("", "")
    if _is_any(label, STUDY_LOCATION_HINTS):
        if education and education.location:
            return education.location, "主档案.education.location"
        saved = _saved_answer(field, profile)
        return (saved, "主档案.application_answers") if saved else ("", "")
    if _is_any(label, PREFERRED_LOCATION_HINTS):
        cities = profile.target_cities if field.multiple else profile.target_cities[:1]
        if cities:
            return ", ".join(cities), "主档案.target_cities"
        saved = _saved_answer(field, profile)
        return (saved, "主档案.application_answers") if saved else ("", "")
    if _is_any(label, CURRENT_LOCATION_HINTS):
        return (profile.location, "主档案.location") if profile.location else ("", "")
    if _is_any(label, SKILL_HINTS) and profile.skills:
        return ", ".join(profile.skills), "主档案.skills"
    if _is_any(label, LANGUAGE_HINTS) and profile.languages:
        return ", ".join(profile.languages), "主档案.languages"
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
    options = [option for option in options if _normalized(option) not in PLACEHOLDER_OPTIONS]
    wanted = _normalized(value)
    for option in options:
        if _normalized(option) == wanted:
            return option
    wanted_alias = next((index for index, group in enumerate(OPTION_ALIASES)
                         if wanted in {_normalized(item) for item in group}), -1)
    if wanted_alias >= 0:
        aliases = [option for option in options if _normalized(option) in {
            _normalized(item) for item in OPTION_ALIASES[wanted_alias]
        }]
        return aliases[0] if len(aliases) == 1 else ""
    partial = [option for option in options
               if wanted and (wanted in _normalized(option) or _normalized(option) in wanted)]
    return partial[0] if len(partial) == 1 else ""


def _matching_options(values: list[str], options: list[str]) -> list[str]:
    matched: list[str] = []
    for value in values:
        option = _matching_option(value, options)
        if option and option not in matched:
            matched.append(option)
    return matched


def _is_any(text: str, hints: tuple[str, ...]) -> bool:
    return any(hint in text for hint in hints)


def _surface_when_empty(field: PageField) -> bool:
    text = _field_text(field)
    return field.field_type in {"radio", "checkbox", "select-one", "select-multiple", "combobox"} or _is_any(
        text, OPTIONAL_REVIEW_HINTS
    )


def _local_safe_plan(snapshot: BrowserSnapshot, profile: CandidateProfile) -> FormPlan:
    actions: list[FillAction] = []
    missing: list[str] = []
    cities = [*profile.target_cities, profile.location]
    for field in snapshot.fields:
        label = _field_text(field)
        sensitive = any(hint in label for hint in SENSITIVE_HINTS)
        saved_answer = _saved_answer(field, profile)
        if field.field_type == "section-button":
            action = FillAction(selector=field.selector, label=field.label or field.group_label,
                                action="ask_user", confidence=1,
                                reason="网页中的可选资料栏目尚未展开，请先展开后再分析其中字段")
        elif field.field_type == "file":
            action = FillAction(selector=field.selector, label=field.label or field.name, action="skip",
                                reason="文件由前端的简历选择器单独上传", confidence=1)
        elif _is_third_party(field):
            action = FillAction(selector=field.selector, label=field.label or field.group_label or field.name,
                                action="ask_user", reason="第三方联系信息不得使用候选人本人资料",
                                sensitive=True, confidence=1)
        elif sensitive:
            suggestion = ""
            if any(hint in label for hint in ("gender", "sex", "性别")) and profile.gender != "未识别":
                suggestion = f"；主档案记录为“{profile.gender}”，请从网页的真实选项中确认"
            action = FillAction(selector=field.selector, label=field.label or field.name, action="ask_user",
                                reason=f"敏感或同意类字段需要用户确认{suggestion}", sensitive=True, confidence=1)
        elif _needs_manual_decision(field):
            action = (_manual_answer_action(field, saved_answer) if saved_answer else
                      FillAction(selector=field.selector, label=field.label or field.group_label or field.name,
                                 action="ask_user", reason="是否/偏好类问题需要用户明确选择",
                                 confidence=1))
        elif field.field_type in {"checkbox", "radio"}:
            value, source = _direct_profile_value(field, profile)
            profile_values = [item.strip() for item in re.split(r"[,，、\n]", value) if item.strip()]
            option = field.option_label or field.option_value or field.label
            option_match = next((item for item in profile_values if _matching_option(item, [option])), "")
            city = next((city for city in cities if city and city.casefold() in label), "")
            if option_match:
                action = FillAction(selector=field.selector, label=field.label or field.name, action="check",
                                    value=True, value_source=source, confidence=.99,
                                    reason="该选项与已确认主档案一致")
            elif city:
                action = FillAction(selector=field.selector, label=field.label or field.name, action="check",
                                    value=True, value_source="主档案.target_cities/location", confidence=.95)
            else:
                kind = "skip" if value else ("ask_user" if field.required or _surface_when_empty(field) else "skip")
                action = FillAction(selector=field.selector, label=field.label or field.name, action=kind,
                                    reason=("该选项不在已确认主档案值中"
                                            if value else "单选或复选含义无法从主档案确定"), confidence=1)
        else:
            value, source = _direct_profile_value(field, profile)
            selection_mismatch = False
            if value and field.field_type in {"select-one", "select-multiple", "combobox"}:
                raw_values = [item.strip() for item in re.split(r"[,，、\n]", value) if item.strip()]
                if field.options:
                    matched = _matching_options(raw_values, field.options)
                    selection_mismatch = not matched or (field.multiple and len(matched) != len(raw_values))
                    value = ", ".join(matched if field.multiple else matched[:1])
                elif field.field_type != "combobox":
                    value = ""
                elif not field.multiple:
                    value = raw_values[0] if raw_values else ""
            if value:
                action = FillAction(selector=field.selector, label=field.label or field.name,
                                    action="select" if field.field_type.startswith("select") or field.field_type == "combobox" else "fill",
                                    value=value, value_source=source, confidence=.9 if field.field_type == "combobox" and not field.options else .99,
                                    reason="执行时会再次读取网页选项并回读验证" if field.field_type == "combobox" else "")
            else:
                kind = "ask_user" if field.required or _surface_when_empty(field) else "skip"
                action = FillAction(selector=field.selector, label=field.label or field.name, action=kind,
                                    reason=("主档案值在网页真实选项中没有唯一匹配，请人工选择"
                                            if selection_mismatch else
                                            "主档案中没有可直接确认的值，请从网页真实选项中选择"
                                            if field.options else "主档案中没有可直接确认的值"), confidence=1)
        actions.append(action)
        if field.required and action.action == "ask_user":
            missing.append(field.label or field.name or field.field_type)
    site_type = next((site for site in ("lever", "greenhouse", "workday") if site in snapshot.url.lower()), "generic")
    return FormPlan(page_summary="本地安全映射已生成；不明确的字段已保留给用户确认。",
                    site_type=site_type, actions=actions, missing_questions=missing)


def create_local_form_plan(snapshot: BrowserSnapshot, profile: CandidateProfile) -> FormPlan:
    """Fast deterministic plan used for immediate ATS/resume reconciliation."""
    return _local_safe_plan(snapshot, profile)


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
        if field.field_type == "section-button":
            guarded.append(FillAction(selector=field.selector, label=label, action="ask_user", value="",
                                      confidence=1, reason="网页中的可选资料栏目尚未展开，请先展开后再分析其中字段"))
            continue
        if any(hint in text for hint in SENSITIVE_HINTS):
            suggestion = ""
            if any(hint in text for hint in ("gender", "sex", "性别")) and profile.gender != "未识别":
                suggestion = f"；主档案记录为“{profile.gender}”，请从网页的真实选项中确认"
            guarded.append(FillAction(selector=field.selector, label=label, action="ask_user", value="",
                                      confidence=1, sensitive=True,
                                      reason=f"敏感或声明类字段必须由用户确认{suggestion}"))
            continue
        if _needs_manual_decision(field):
            saved = _saved_answer(field, profile)
            guarded.append(_manual_answer_action(field, saved) if saved else
                           FillAction(selector=field.selector, label=label, action="ask_user", value="",
                                      confidence=1, reason="是否/偏好类问题需要用户明确选择"))
            continue
        direct_value, _ = _direct_profile_value(field, profile)
        contextual_location = any(_is_any(text, hints) for hints in (
            COUNTRY_HINTS, STUDY_LOCATION_HINTS, PREFERRED_LOCATION_HINTS, CURRENT_LOCATION_HINTS,
        ))
        if contextual_location and not direct_value:
            guarded.append(FillAction(selector=field.selector, label=label, action="ask_user", value="",
                                      confidence=1, reason="对应的地点资料尚未确认，不能借用其他地点字段"))
            continue
        if action.action == "select" and field.options:
            values = [item.strip() for item in re.split(r"[,，\n]", str(action.value)) if item.strip()]
            selected_values = _matching_options(values, field.options)
            if not selected_values or (field.multiple and len(selected_values) != len(values)):
                guarded.append(FillAction(selector=field.selector, label=label, action="ask_user", value="",
                                          confidence=1, reason="建议值与网页真实选项不一致"))
                continue
            action.value = ", ".join(selected_values if field.multiple else selected_values[:1])
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


def _comparison_parts(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，、\n]", value) if item.strip()]


def _comparison_values_match(expected: str, actual: str) -> bool:
    expected_parts = _comparison_parts(expected)
    actual_parts = _comparison_parts(actual)
    if not expected_parts or not actual_parts or len(expected_parts) != len(actual_parts):
        return False
    return all(any(_matching_option(wanted, [candidate]) for candidate in actual_parts)
               for wanted in expected_parts)


def _checked(value: str | bool) -> bool:
    return value is True or _normalized(str(value)) in {"true", "1", "yes", "是"}


def build_form_review(snapshot: BrowserSnapshot, plan: FormPlan) -> FormReviewResult:
    """Compare the ATS draft against the plan backed by the confirmed profile."""
    reviewed_plan = plan.model_copy(deep=True)
    actions = {action.selector: action for action in reviewed_plan.actions}
    groups: dict[str, list[PageField]] = {}
    for field in snapshot.fields:
        if field.field_type == "radio":
            key = f"radio:{_normalized(field.group_label or field.name or field.label)}"
        elif field.field_type == "checkbox" and field.group_label:
            key = f"checkbox:{_normalized(field.group_label)}"
        else:
            key = field.selector
        groups.setdefault(key, []).append(field)

    comparisons: list[FieldComparison] = []
    for key, fields in groups.items():
        group_actions = [actions[field.selector] for field in fields if field.selector in actions]
        representative = fields[0]
        label = representative.group_label or representative.label or representative.name or representative.field_type
        toggle_group = representative.field_type in {"radio", "checkbox"}
        if toggle_group:
            options = list(dict.fromkeys(
                option for field in fields
                for option in ([field.option_label or field.option_value or field.label] + field.options)
                if option
            ))
            selected = [field.option_label or field.option_value or field.label for field in fields
                        if _normalized(field.current_value) == "true"]
            expected = [field.option_label or field.option_value or field.label for field in fields
                        if (action := actions.get(field.selector)) and action.action == "check" and _checked(action.value)]
            site_value = ", ".join(selected)
            expected_value = ", ".join(expected)
        else:
            options = representative.options
            site_value = representative.current_value.strip()
            if _normalized(site_value) in PLACEHOLDER_OPTIONS:
                site_value = ""
            expected_action = next((action for action in group_actions
                                    if action.action in {"fill", "select", "check"}), None)
            expected_value = str(expected_action.value) if expected_action else ""

        source = next((action.value_source for action in group_actions if action.value_source), "")
        manual = any(action.action == "ask_user" or action.sensitive for action in group_actions)
        option_problem = any("选项" in action.reason and "没有唯一匹配" in action.reason
                             for action in group_actions)
        if representative.field_type == "file":
            status = "matched" if site_value else "unmapped"
            recommendation = "简历文件已在招聘网页中" if site_value else "可以先让招聘网站解析所选简历"
        elif option_problem:
            status = "option_unavailable"
            recommendation = "档案值无法唯一对应网页选项，请从网页真实选项中选择"
        elif manual:
            status = "manual_review"
            recommendation = "网站已有值也不能直接信任，请由用户根据真实情况确认"
        elif expected_value and not site_value:
            status = "missing"
            recommendation = "招聘网站没有填出该项，智达将使用已确认主档案补齐"
        elif expected_value and _comparison_values_match(expected_value, site_value):
            status = "matched"
            recommendation = "网站解析结果与主档案一致，无需重复填写"
            for action in group_actions:
                if action.action in {"fill", "select", "check"}:
                    action.action = "skip"
                    action.reason = "招聘网站已正确填写，经主档案核对一致"
        elif expected_value and site_value:
            status = "conflict"
            recommendation = "网站解析结果与已确认主档案冲突，智达将按主档案纠正"
            for action in group_actions:
                if action.action in {"fill", "select", "check"}:
                    action.reason = "网站解析值与已确认主档案不一致，执行时将纠正并回读"
        elif site_value:
            status = "manual_review"
            recommendation = "网站填出了值，但主档案没有可靠依据，请人工核对"
        else:
            status = "unmapped"
            recommendation = "网站和主档案都没有可靠值，需要用户补充"

        comparisons.append(FieldComparison(
            key=key, selector=representative.selector, label=label,
            field_type=representative.field_type, required=any(field.required for field in fields),
            options=options, site_value=site_value, expected_value=expected_value,
            value_source=source, status=status, recommendation=recommendation,
        ))

    summary = ComparisonSummary()
    for item in comparisons:
        setattr(summary, item.status, getattr(summary, item.status) + 1)
    return FormReviewResult(snapshot=snapshot, plan=reviewed_plan,
                            comparisons=comparisons, summary=summary)
