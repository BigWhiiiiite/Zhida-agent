from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from .models import FieldEvidence, ResumeProfile
from .storage import create_conflict, get_profile, get_resume, save_profile, update_resume


SCALAR_FIELDS = [
    "name", "english_name", "gender", "birth_date", "age", "phone", "email", "qq", "wechat",
    "location", "hometown", "website", "github", "linkedin", "target_role", "available_date",
    "internship_duration", "days_per_week", "expected_salary", "remote_preference", "summary",
]
LIST_FIELDS = ["skills", "languages", "certificates", "awards", "target_industries", "target_cities"]

NON_REUSABLE_ANSWER_HINTS = (
    "consent", "agree", "privacy", "terms", "legal", "declaration", "authorization", "visa",
    "sponsorship", "salary", "compensation", "gender", "sex", "race", "ethnicity", "disability",
    "veteran", "referral", "available", "availability", "work permit", "right to work",
    "同意", "隐私", "条款", "声明", "工作许可", "签证", "担保", "薪资", "薪酬", "性别",
    "种族", "族裔", "残障", "退伍", "调剂", "内推", "政治面貌", "户口", "婚姻", "身份证",
    "到岗", "可入职",
)


def _answer_profile_field(question: str, field_name: str) -> str:
    description = f"{question} {field_name}".casefold()
    mappings = (
        (("qq", "qq号", "qq号码", "qq account"), "qq"),
        (("wechat", "weixin", "微信"), "wechat"),
        (("email", "e-mail", "邮箱", "电子邮件"), "email"),
        (("phone", "mobile", "telephone", "手机", "电话"), "phone"),
        (("linkedin",), "linkedin"),
        (("github",), "github"),
        (("portfolio", "personal website", "个人网站", "作品集"), "website"),
    )
    return next((field for hints, field in mappings if any(hint in description for hint in hints)), "")


def save_application_answer(question: str, field_name: str, value: str):
    """Persist an explicit reusable answer without learning legal/sensitive decisions."""
    cleaned_question = re.sub(r"\s+", " ", question).strip().strip("*✱ ")
    cleaned_value = value.strip()
    description = f"{cleaned_question} {field_name}".casefold()
    if any(hint in description for hint in NON_REUSABLE_ANSWER_HINTS):
        raise ValueError("这类敏感或本次申请答案不会保存到可复用主档案")
    if not cleaned_question or not cleaned_value:
        raise ValueError("问题和答案不能为空")
    current = ResumeProfile.model_validate(get_profile().model_dump())
    direct_field = _answer_profile_field(cleaned_question, field_name)
    if direct_field:
        setattr(current, direct_field, cleaned_value)
    else:
        current.application_answers[cleaned_question] = cleaned_value
    return save_profile(current)


def detect_language(text: str) -> str:
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if chinese > 40 and latin > 80:
        return "中英混合"
    if chinese > 40:
        return "中文"
    if latin > 80:
        return "英文"
    return "未识别"


def _source_line(text: str, value: Any) -> str:
    needle = str(value).strip()
    if not needle:
        return ""
    for line in text.splitlines():
        if needle.lower() in line.lower():
            return line.strip()[:500]
    return needle[:500]


def build_evidence(profile: ResumeProfile, text: str, parser: str) -> list[FieldEvidence]:
    items: list[FieldEvidence] = []
    for field in SCALAR_FIELDS:
        value = getattr(profile, field)
        if value in (None, "", "未识别"):
            continue
        exact = str(value).lower() in text.lower()
        confidence = 0.97 if exact and field in {"email", "phone"} else (0.88 if exact else 0.68)
        if parser != "local-rules":
            confidence = max(confidence, 0.82)
        items.append(FieldEvidence(id=str(uuid4()), field_path=field, value=value, confidence=confidence,
                                   source_text=_source_line(text, value)))
    for field in ("education", "internships", "projects", "skills"):
        values = getattr(profile, field)
        if not values:
            continue
        items.append(FieldEvidence(id=str(uuid4()), field_path=field,
            value=[v.model_dump() if hasattr(v, "model_dump") else v for v in values], confidence=0.7,
            source_text="\n".join(text.splitlines()[:80])[:1200]))
    return items


def _blank(value: Any) -> bool:
    return value in (None, "", "未识别", [])


def merge_into_profile(incoming: ResumeProfile, resume_id: str) -> None:
    current_model = get_profile()
    current = ResumeProfile.model_validate(current_model.model_dump())
    changed = False
    for field in SCALAR_FIELDS:
        old, new = getattr(current, field), getattr(incoming, field)
        if _blank(new):
            continue
        if _blank(old):
            setattr(current, field, new); changed = True
        elif old != new:
            create_conflict(field, old, new, resume_id)
    for field in LIST_FIELDS:
        old_list = list(getattr(current, field)); new_list = getattr(incoming, field)
        for value in new_list:
            if value and value not in old_list:
                old_list.append(value); changed = True
        setattr(current, field, old_list)
    for field, key_fields in (("education", ("school", "major")), ("internships", ("organization", "role")), ("projects", ("name",))):
        old_list = list(getattr(current, field))
        for value in getattr(incoming, field):
            duplicate = any(all(getattr(existing, key, "") == getattr(value, key, "") for key in key_fields) for existing in old_list)
            if not duplicate:
                old_list.append(value); changed = True
        setattr(current, field, old_list)
    if changed:
        save_profile(current)


def _updated_profile(profile: ResumeProfile, field_path: str, value: Any) -> ResumeProfile:
    if field_path not in ResumeProfile.model_fields:
        raise ValueError(f"不支持修改字段：{field_path}")
    data = profile.model_dump()
    data[field_path] = value
    return ResumeProfile.model_validate(data)


def apply_profile_value(field_path: str, value: Any) -> None:
    current = ResumeProfile.model_validate(get_profile().model_dump())
    save_profile(_updated_profile(current, field_path, value))


def sync_edited_profile_value(resume_id: str, field_path: str, value: Any) -> None:
    """Apply an explicit user edit to both its resume version and the master profile."""
    resume = get_resume(resume_id)
    if not resume:
        raise ValueError("简历不存在")
    updated_resume = _updated_profile(resume.profile, field_path, value)
    update_resume(resume_id, profile=updated_resume)
    apply_profile_value(field_path, value)
