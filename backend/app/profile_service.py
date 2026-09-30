from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from .field_semantics import normalize_text, option_fingerprint
from .models import ApplicationAnswerMemory, FieldEvidence, ResumeProfile
from .storage import create_conflict, get_profile, get_resume, save_profile, update_resume


SCALAR_FIELDS = [
    "name", "english_name", "gender", "birth_date", "age", "phone", "email", "qq", "wechat",
    "location", "country_region", "nationality", "ethnicity", "political_status", "marital_status",
    "hometown", "hukou_location", "address", "website", "github", "linkedin", "target_role",
    "available_date", "internship_duration", "days_per_week", "expected_salary", "remote_preference",
    "willing_to_relocate", "campus_candidate_type", "summary",
]
LIST_FIELDS = [
    "skills", "languages", "certificates", "awards", "target_industries", "target_cities",
    "preferred_business_groups", "interview_preferences",
]

SEMANTIC_PROFILE_FIELDS = {
    "candidate.english_name": "english_name", "candidate.age": "age",
    "candidate.birth_date": "birth_date",
    "candidate.email": "email", "candidate.phone": "phone", "candidate.wechat": "wechat",
    "candidate.qq": "qq", "candidate.github": "github", "candidate.linkedin": "linkedin",
    "candidate.website": "website", "candidate.country_region": "country_region",
    "candidate.nationality": "nationality", "candidate.ethnicity": "ethnicity",
    "candidate.political_status": "political_status", "candidate.marital_status": "marital_status",
    "candidate.hukou_location": "hukou_location", "candidate.address": "address",
    "candidate.current_location": "location", "candidate.campus_type": "campus_candidate_type",
    "candidate.skills": "skills", "candidate.languages": "languages",
    "preference.work_location": "target_cities",
    "preference.business_group": "preferred_business_groups",
    "preference.interview_location": "interview_preferences",
    "preference.relocation": "willing_to_relocate",
}

NON_REUSABLE_ANSWER_HINTS = (
    "consent", "agree", "privacy", "terms", "legal", "declaration", "authorization", "visa",
    "sponsorship", "salary", "compensation", "gender", "sex", "race", "ethnicity", "disability",
    "veteran", "referral", "available", "availability", "work permit", "right to work",
    "同意", "隐私", "条款", "声明", "工作许可", "签证", "担保", "薪资", "薪酬", "性别",
    "种族", "族裔", "残障", "退伍", "内推", "政治面貌", "户口", "婚姻", "身份证",
    "到岗", "可入职",
    "身份证", "证件号码", "证件号", "护照号码", "护照号", "实名认证", "national id", "id number", "passport number",
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
        (("country/region", "country or region", "国家/地区", "所在国家", "国家或地区"), "country_region"),
        (("国籍", "citizenship", "nationality"), "nationality"),
        (("民族", "族别", "ethnicity", "ethnic group"), "ethnicity"),
        (("政治面貌", "political status", "political affiliation"), "political_status"),
        (("婚姻状况", "marital status"), "marital_status"),
        (("户籍所在地", "户口所在地", "户籍地", "hukou"), "hukou_location"),
        (("通讯地址", "联系地址", "mailing address"), "address"),
        (("current location", "current city", "当前所在地", "当前所处地", "现居地", "居住地"), "location"),
        (("preferred location", "preferred city", "work city", "期望工作城市", "期望城市", "意向城市"), "target_cities"),
        (("感兴趣的事业群", "意向事业群", "business group"), "preferred_business_groups"),
        (("面试城市", "面试地点", "interview city", "interview location"), "interview_preferences"),
        (("接受其他城市分配", "接受调剂", "willing to relocate"), "willing_to_relocate"),
        (("ai application skill", "ai skills", "technical skills", "专业技能", "技术技能", "ai应用技能"), "skills"),
        (("language ability", "language skills", "languages", "语言能力", "外语能力"), "languages"),
    )
    return next((field for hints, field in mappings if any(hint in description for hint in hints)), "")


def save_application_answer(question: str, field_name: str, value: str, *, semantic_key: str = "",
                            entity_scope: str = "", field_signature: str = "",
                            field_type: str = "text", options: list[str] | None = None,
                            source_url: str = ""):
    """Persist an explicit reusable answer without learning legal/sensitive decisions."""
    cleaned_question = re.sub(r"\s+", " ", question).strip().strip("*✱ ")
    cleaned_value = value.strip()
    description = f"{cleaned_question} {field_name}".casefold()
    semantic_key = semantic_key.strip() or "application.custom"
    entity_scope = entity_scope.strip() or "application"
    if (any(hint in description for hint in NON_REUSABLE_ANSWER_HINTS)
            or semantic_key == "third_party.contact" or entity_scope == "third_party"):
        raise ValueError("这类敏感或本次申请答案不会保存到可复用主档案")
    if not cleaned_question or not cleaned_value:
        raise ValueError("问题和答案不能为空")
    current = ResumeProfile.model_validate(get_profile().model_dump())
    direct_field = SEMANTIC_PROFILE_FIELDS.get(semantic_key) or _answer_profile_field(cleaned_question, field_name)
    if direct_field:
        if direct_field == "age":
            age_text = re.sub(r"(?:周岁|岁)$", "", cleaned_value).strip()
            if not age_text.isdecimal() or not 0 <= int(age_text) <= 120:
                raise ValueError("年龄请填写 0 到 120 之间的整数周岁")
            current.age = int(age_text)
        elif direct_field in LIST_FIELDS:
            values = [item.strip() for item in re.split(r"[,，、\n]", cleaned_value) if item.strip()]
            setattr(current, direct_field, values)
        else:
            setattr(current, direct_field, cleaned_value)
    elif not semantic_key.startswith(("education.", "experience.", "project.")):
        current.application_answers[cleaned_question] = cleaned_value
    normalized_question = normalize_text(cleaned_question)
    fingerprint = option_fingerprint(options or [])
    memory_key = field_signature.strip() or "|".join((
        semantic_key, entity_scope, normalized_question, field_type, fingerprint,
    ))
    now = datetime.now(timezone.utc)
    existing = next((item for item in current.application_answer_memory
                     if (item.field_signature or "|".join((
                         item.semantic_key, item.entity_scope, item.normalized_question,
                         item.field_type, item.option_fingerprint,
                     ))) == memory_key), None)
    source_host = (urlparse(source_url).hostname or "").casefold()
    if existing:
        existing.question = cleaned_question
        existing.normalized_question = normalized_question
        existing.semantic_key = semantic_key
        existing.entity_scope = entity_scope
        existing.field_signature = field_signature.strip()
        existing.field_type = field_type
        existing.option_fingerprint = fingerprint
        existing.value = cleaned_value
        existing.source_host = source_host or existing.source_host
        existing.confirmed_count += 1
        existing.updated_at = now
    else:
        current.application_answer_memory.append(ApplicationAnswerMemory(
            id=str(uuid4()), question=cleaned_question, normalized_question=normalized_question,
            semantic_key=semantic_key, entity_scope=entity_scope,
            field_signature=field_signature.strip(), field_type=field_type,
            option_fingerprint=fingerprint, value=cleaned_value, source_host=source_host,
            updated_at=now,
        ))
    current.application_answer_memory = sorted(
        current.application_answer_memory, key=lambda item: item.updated_at, reverse=True,
    )[:500]
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
