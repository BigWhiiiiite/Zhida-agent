"""Explicit, version-bound human facts, not guessed answers to site questions.

This channel edits a single named record in the deliberately selected CV.  It
does not broaden answer-memory matching or silently update another CV/master.
"""
from __future__ import annotations

from calendar import monthrange
import hashlib
import json
import re
import unicodedata
from collections import Counter
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from .models import FieldEvidence, ResumeProfile, ResumeRecord


SECTION_LABELS = {"education": "教育经历", "internships": "实习经历", "projects": "项目经历"}
IDENTITIES = {
    "education": ("school", "degree", "start_date"),
    "internships": ("organization", "role", "start_date"),
    "projects": ("name", "start_date"),
}
# Rankings, unified admission, consents and site-specific choices deliberately
# are NOT facts here. They retain the option/question-scoped memory channel.
ATTRIBUTES = {
    "education": {
        "school": "学校", "college": "学院", "degree": "学历/学位", "major": "专业",
        "advisor": "导师", "laboratory": "实验室", "research_direction": "研究方向",
        "location": "就读地点", "start_date": "入学时间", "end_date": "毕业时间",
        "academic_system": "学制", "student_id": "学号", "gpa": "GPA", "description": "教育描述",
    },
    "internships": {
        "organization": "单位", "department": "部门", "role": "岗位/职责", "location": "工作地点",
        "start_date": "开始时间", "end_date": "结束时间", "description": "实习描述",
    },
    "projects": {
        "name": "项目名称", "role": "项目角色", "start_date": "开始时间", "end_date": "结束时间",
        "background": "项目背景", "description": "项目描述", "responsibilities": "项目中职责",
        "project_url": "项目网址", "github_url": "代码网址",
    },
}


class FactRevisionConflict(ValueError):
    pass


class FactAttribute(BaseModel):
    key: str
    label: str
    value: str


class FactRecordTarget(BaseModel):
    record_key: str
    section: Literal["education", "internships", "projects"]
    label: str
    attributes: list[FactAttribute]


class FactTargets(BaseModel):
    resume_id: str
    revision: str
    records: list[FactRecordTarget] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ConfirmedFactRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    record_key: str = Field(pattern=r"^fact_[a-f0-9]{64}$")
    attribute: str = Field(min_length=1, max_length=50)
    value: str = Field(min_length=1, max_length=4000)
    confirmed: StrictBool

    @field_validator("confirmed")
    @classmethod
    def explicit_confirmation(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("只有本人明确确认的真实信息可以保存为简历事实")
        return value


def _digest(payload: object) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def resume_revision(resume: ResumeRecord) -> str:
    return _digest({"resume_id": resume.id, "updated_at": resume.updated_at.isoformat(),
                    "profile": resume.profile.model_dump(mode="json"),
                    "evidence": [item.model_dump(mode="json") for item in resume.evidence]})


def _identity(section: str, record: BaseModel) -> tuple[str, ...]:
    return tuple(re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(getattr(record, key))).strip()).casefold()
                 for key in IDENTITIES[section])


def _record_key(resume_id: str, section: str, record: BaseModel) -> str:
    return "fact_" + _digest({"resume_id": resume_id, "section": section, "identity": _identity(section, record)})


def _targets(resume: ResumeRecord) -> tuple[list[FactRecordTarget], list[str]]:
    targets, warnings = [], []
    for section, labels in ATTRIBUTES.items():
        records = getattr(resume.profile, section)
        identities = Counter(_identity(section, item) for item in records)
        for item in records:
            identity = _identity(section, item)
            if not identity[0]:
                warning = f"{SECTION_LABELS[section]}中有记录缺少名称，请先在简历资料库补充并核对，不能按顺序猜测记录。"
                if warning not in warnings:
                    warnings.append(warning)
                continue
            if identities[identity] != 1:
                warning = f"{SECTION_LABELS[section]}中存在身份相同的记录，请先在简历资料库区分名称、角色/学历及开始时间。"
                if warning not in warnings:
                    warnings.append(warning)
                continue
            label = " · ".join(str(getattr(item, key)).strip() for key in IDENTITIES[section]
                               if str(getattr(item, key)).strip())
            targets.append(FactRecordTarget(record_key=_record_key(resume.id, section, item), section=section,
                                           label=f"{SECTION_LABELS[section]}：{label}", attributes=[
                                               FactAttribute(key=key, label=label, value=getattr(item, key))
                                               for key, label in labels.items()]))
    return targets, warnings


def build_fact_targets(resume: ResumeRecord) -> FactTargets:
    targets, warnings = _targets(resume)
    if resume.status in {"pending", "parsing", "failed"}:
        return FactTargets(resume_id=resume.id, revision=resume_revision(resume),
                           warnings=["请先完成简历解析及资料检查，再补充事实。"])
    return FactTargets(resume_id=resume.id, revision=resume_revision(resume), records=targets, warnings=warnings)


def _validate_value(attribute: str, value: str) -> str:
    value = value.strip()
    if not value or any(ord(char) < 32 and char not in "\n\t" for char in value):
        raise ValueError("请填写明确的真实信息，不接受空值或控制字符")
    if attribute not in {"description", "background", "responsibilities"} and len(value) > 500:
        raise ValueError("该字段最多支持 500 个字符")
    if attribute in {"start_date", "end_date"}:
        if attribute == "end_date" and value == "至今":
            return value
        if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
            raise ValueError("日期请按已知精度填写 YYYY、YYYY-MM 或 YYYY-MM-DD；结束时间也可填至今，不会自动补月或日")
        parts = value.split("-")
        try:
            date(int(parts[0]), int(parts[1]) if len(parts) > 1 else 1, int(parts[2]) if len(parts) > 2 else 1)
        except ValueError as exc:
            raise ValueError("日期无效，请核对真实年月日") from exc
    if attribute in {"project_url", "github_url"} and not re.match(r"^https?://[^\s]+$", value):
        raise ValueError("项目网址只接受完整的 http 或 https 网址")
    return value


def _date_bounds(value: str) -> tuple[date, date] | None:
    """Bound known precision for comparison only; never invent stored dates."""
    value = value.strip()
    if not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value):
        return None  # Empty, legacy free text and '至今' have no finite bounds.
    parts = [int(part) for part in value.split("-")]
    try:
        year = parts[0]
        if len(parts) == 1:
            return date(year, 1, 1), date(year, 12, 31)
        month = parts[1]
        if len(parts) == 2:
            return date(year, month, 1), date(year, month, monthrange(year, month)[1])
        exact = date(year, month, parts[2])
        return exact, exact
    except ValueError:
        return None  # An invalid legacy counterpart cannot establish ordering.


def prepare_confirmed_fact(resume: ResumeRecord, request: ConfirmedFactRequest
                           ) -> tuple[ResumeProfile, list[FieldEvidence], dict[str, str]]:
    """Resolve the server-side identity and change exactly one scalar field."""
    if request.confirmed is not True:
        raise ValueError("只有本人明确确认的真实信息可以保存为简历事实")
    if request.revision != resume_revision(resume):
        raise FactRevisionConflict("简历已发生变化，请刷新补充信息列表后重新确认，旧记录不会被覆盖")
    if resume.status in {"pending", "parsing", "failed"}:
        raise ValueError("请先完成简历解析及资料检查")
    targets, _ = _targets(resume)
    matching_targets = [item for item in targets if item.record_key == request.record_key]
    if len(matching_targets) != 1:
        raise ValueError("无法唯一确定这条经历，请刷新并在简历资料库核对记录身份")
    section = matching_targets[0].section
    if request.attribute not in ATTRIBUTES[section]:
        raise ValueError("这不是可保存的经历事实；网站排名选项、统招、调剂和声明应在原题中单独确认")
    value = _validate_value(request.attribute, request.value)
    profile = resume.profile.model_copy(deep=True)
    matches = [item for item in getattr(profile, section) if _record_key(resume.id, section, item) == request.record_key]
    if len(matches) != 1:
        raise ValueError("存在重复经历，不能按数组位置选择记录")
    target = matches[0]
    previous = str(getattr(target, request.attribute))
    setattr(target, request.attribute, value)
    if request.attribute in {"start_date", "end_date"}:
        start, end = _date_bounds(target.start_date), _date_bounds(target.end_date)
        if start is not None and end is not None and end[1] < start[0]:
            raise ValueError("结束时间不能早于开始时间，请核对这条经历的真实起止时间，未保存任何修改")
    if not _identity(section, target)[0] or sum(_identity(section, item) == _identity(section, target)
                                              for item in getattr(profile, section)) != 1:
        raise ValueError("修改后会产生身份相同的经历，请先区分记录，未保存任何修改")
    # Keep all existing review states/source quotes; a single fact confirmation
    # must not launder an entire unreviewed section into confirmed evidence.
    evidence = [item.model_copy(deep=True) for item in resume.evidence]
    for item in evidence:
        if item.field_path == section:
            item.value = [record.model_dump(mode="json") for record in getattr(profile, section)]
    audit = {"section": section, "record_key": request.record_key,
             "new_record_key": _record_key(resume.id, section, target), "attribute": request.attribute,
             "previous_value": previous, "value": value, "revision": request.revision,
             "source": "user_explicit_confirmation"}
    return profile, evidence, audit


def get_fact_targets(resume_id: str) -> FactTargets:
    from .storage import get_resume
    resume = get_resume(resume_id)
    if not resume:
        raise LookupError("简历不存在")
    return build_fact_targets(resume)


def save_confirmed_fact(resume_id: str, request: ConfirmedFactRequest) -> ResumeRecord:
    from .storage import update_confirmed_resume_fact
    return update_confirmed_resume_fact(resume_id, request)
