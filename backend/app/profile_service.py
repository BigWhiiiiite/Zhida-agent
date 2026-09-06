from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

from .models import FieldEvidence, ResumeProfile
from .storage import create_conflict, get_profile, save_profile


SCALAR_FIELDS = [
    "name", "english_name", "gender", "birth_date", "age", "phone", "email", "wechat",
    "location", "hometown", "website", "github", "linkedin", "target_role", "available_date",
    "internship_duration", "days_per_week", "expected_salary", "remote_preference", "summary",
]
LIST_FIELDS = ["skills", "languages", "certificates", "awards", "target_industries", "target_cities"]


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


def apply_profile_value(field_path: str, value: Any) -> None:
    if field_path not in SCALAR_FIELDS:
        return
    current = get_profile()
    profile = ResumeProfile.model_validate(current.model_dump())
    setattr(profile, field_path, value)
    save_profile(profile)

