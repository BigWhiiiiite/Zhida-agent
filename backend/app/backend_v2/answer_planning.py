"""D2 read-only planning: evidence is not permission and retrieval is not a fact.

Input recognition must be produced by our normalizer; facts/entities come from an
owned, version-pinned database read. This module has no model, browser or storage
client. It deliberately emits *no executable actions*, including on success.
"""
from __future__ import annotations

import calendar
import json
import math
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from .form_recognition import MAX_FIELDS


def _normal(value: Any) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value))).casefold().strip("*:：")


def _precision(value: Any) -> Any:
    # Existing BrowserModels use HTML input type 'date' for day precision.
    return "day" if value == "date" else value


# Full-question aliases only. In particular, '是', '否', '名称' and '城市' have
# no meaning by themselves. No substring or vector-similarity answer matching.
_LABELS = {
    "name": ("姓名", "真实姓名", "中文姓名", "本人姓名", "full name"),
    "email": ("邮箱", "电子邮箱", "电子邮件", "email"),
    "phone": ("手机", "手机号", "手机号码", "移动电话", "联系电话", "mobile phone"),
    "gender": ("性别", "gender"),
    "birth_date": ("出生日期", "生日", "date of birth"),
    "ethnicity": ("民族",),
    "height_cm": ("身高", "身高(cm)", "身高(厘米)"),
    "weight_kg": ("体重", "体重(kg)", "体重(千克)"),
    "political_status": ("政治面貌",),
    "marital_status": ("婚姻状况",),
    "native_place": ("籍贯",),
    "source_place": ("生源地", "高考所在地"),
    "household_location": ("户口所在地", "户籍所在地"),
    "current_location": ("现居住地", "当前居住地", "当前所在地", "当前所处地"),
    "address": ("通讯地址", "通信地址", "详细居住地址"),
    "emergency_contact_name": ("紧急联系人姓名", "紧急联系人"),
    "emergency_contact_phone": ("紧急联系人电话", "紧急联系人手机号", "紧急联系方式"),
    "desired_work_city": ("意向城市", "意向工作城市", "期望工作城市", "期望工作地点"),
    "interview_city": ("面试城市", "参加面试城市", "意向面试城市"),
    "accept_city_assignment": ("除上述选择外，是否还接受其他城市分配", "是否接受其他城市分配"),
    "early_internship": ("是否可提前实习", "是否可以提前实习"),
    "earliest_start_date": ("最早可实习入职时间", "最早实习时间", "最早可入职时间"),
    "recruitment_source": ("如何得知此次校招", "招聘信息来源", "招聘渠道"),
    "school": ("学校", "学校名称", "毕业院校", "就读学校", "院校名称"),
    "college": ("学院", "学院名称", "院系", "院系名称"),
    "major": ("专业", "专业名称", "所学专业"),
    "degree": ("学历", "教育层次"),
    "academic_degree": ("学位", "所获学位"),
    "education_rank": ("专业排名", "成绩排名"),
    "unified_admission": ("是否统招",),
    "study_location": ("目前就读地", "学校所在地"),
    "start_date": ("开始时间", "开始日期", "入学时间", "入职时间", "项目开始时间"),
    "end_date": ("结束时间", "结束日期", "毕业时间", "离职时间", "项目结束时间"),
    "organization": ("公司名称", "实习公司", "实习单位", "工作单位"),
    "project_name": ("项目名称",),
    "role": ("项目角色", "担任角色", "实习岗位", "担任职位"),
    "description": ("项目描述", "项目介绍", "实习内容", "工作内容"),
}
_LABEL_KEYS = {_normal(label): key for key, labels in _LABELS.items() for label in labels}
_KEY_ALIASES = {
    "full_name": "name", "name_cn": "name", "mobile": "phone", "mobile_phone": "phone",
    "sex": "gender", "birthday": "birth_date", "birthdate": "birth_date",
    "ethnic_group": "ethnicity", "height": "height_cm", "weight": "weight_kg",
    "political_affiliation": "political_status", "marriage": "marital_status",
    "school_name": "school", "college_name": "college", "major_name": "major",
    "education_level": "degree", "start": "start_date", "end": "end_date",
    "emergency_contact.name": "emergency_contact_name", "emergency_contact.phone": "emergency_contact_phone",
}
_ENTITY_KEYS = frozenset({"school", "college", "major", "degree", "academic_degree", "education_rank", "unified_admission",
                          "study_location", "start_date", "end_date", "organization", "project_name", "role", "description"})
_SITE_KEYS = frozenset({"desired_work_city", "interview_city", "accept_city_assignment", "early_internship",
                        "earliest_start_date", "recruitment_source"})
_DEGREE = {"硕士": "master", "硕士研究生": "master", "研究生硕士": "master", "master": "master",
           "masters": "master", "master's": "master", "msc": "master", "本科": "bachelor",
           "学士": "bachelor", "bachelor": "bachelor", "bachelor's": "bachelor", "bsc": "bachelor",
           "博士": "doctor", "博士研究生": "doctor", "phd": "doctor"}
_FORBIDDEN = re.compile(r"声明|承诺|同意|隐私|用户协议|签名|signature|consent|agreement", re.I)


def _key(value: Any) -> str:
    value = str(value or "").strip()
    if value in {"project.name"}:
        return "project_name"
    if value.startswith(("education.", "experience.", "internship.", "project.")):
        value = value.split(".", 1)[1]
    return _KEY_ALIASES.get(value, value)


def _site(value: Any) -> str:
    value = str(value or "").strip().lower().rstrip(".")
    value = value.removeprefix("site:")
    return value if value and not any(c in value for c in "/:@ ") else ""


def _scope_allowed(scope: str, context: dict, subject: str) -> bool:
    return scope in {"global", "resume:" + context["resume_revision_id"],
                     "site:" + _site(context.get("site_scope")), "entity:" + subject}


def _semantic(field: dict) -> str | None:
    key = _LABEL_KEYS.get(_normal(field.get("question", "")))
    # A nested contact name must never become the applicant's name.
    section = "/".join(str(x) for x in field.get("section_path", [])).lower()
    if "紧急联系" in section:
        return {"name": "emergency_contact_name", "phone": "emergency_contact_phone"}.get(key, key)
    if key in {"name", "phone", "email"} and re.search(r"家庭|亲属|推荐人|证明人|联系人|reference|family", section):
        return None
    return key


def _anchor_value(key: str, value: Any) -> str:
    normalized = _normal(value)
    return _DEGREE.get(normalized, normalized) if key == "degree" else normalized


def _subject(field: dict, key: str, context: dict) -> tuple[str | None, str | None]:
    binding = field.get("entity_binding") or {}
    kind = binding.get("kind")
    if kind == "experience":
        section = "/".join(str(x) for x in field.get("section_path", []))
        if not re.search(r"实习|internship", section, re.I):
            return None, "尚未确认这是实习还是正式工作栏目，不能把实习写成正式工作经历。"
        kind = "internship"
    if key not in _ENTITY_KEYS:
        if kind in {"education", "project", "internship"}:
            return None, "该题属于经历栏目，不能借用本人的通用资料。"
        return "person", None
    if binding.get("status") != "record_observed" or kind not in {"education", "project", "internship"}:
        return None, "还不能确定此题对应哪一段教育、实习或项目，需要补读栏目及记录标识。"
    anchors = []
    for anchor in binding.get("anchors", []):
        akey = _key(anchor.get("semantic_key"))
        if akey not in {"school", "college", "major", "degree", "organization", "project_name"}:
            continue
        if anchor.get("value") in (None, "") or not anchor.get("evidence_refs"):
            continue
        anchors.append((akey, _anchor_value(akey, anchor["value"])))
    if not anchors:
        return None, "已看到经历分组，但缺少学校、学历或项目名称等对应依据；不会按列表顺序猜测。"
    matched = []
    for entity in context.get("entities", []):
        if entity.get("user_id", context["user_id"]) != context["user_id"]:
            continue
        if entity.get("resume_revision_id", context["resume_revision_id"]) != context["resume_revision_id"]:
            continue
        if entity.get("entity_type") != kind:
            continue
        content = {_key(k): v for k, v in (entity.get("content") or {}).items()}
        if kind == "project" and "project_name" not in content:
            content["project_name"] = content.get("name")
        if entity.get("education_level"):
            content["degree"] = entity["education_level"]
        if all(content.get(k) not in (None, "") and _anchor_value(k, content[k]) == v for k, v in anchors):
            identity = entity.get("entity_id") or entity.get("id")
            if identity:
                matched.append(str(identity))
    if len(set(matched)) != 1:
        return None, "网页经历标识与所选简历无法唯一对应；本科和硕士、不同项目不会混填。"
    return matched[0], None


def _active(fact: dict, now: datetime) -> bool:
    if fact.get("confirmation") != "confirmed" or not fact.get("revision_id"):
        return False
    expiry = fact.get("valid_until")
    if expiry is None:
        return True
    try:
        expiry = datetime.fromisoformat(expiry.replace("Z", "+00:00")) if isinstance(expiry, str) else expiry
        return expiry.tzinfo is not None and expiry > now
    except (TypeError, ValueError, AttributeError):
        return False


def _safe_alias(key: str, value: Any) -> str:
    value = _normal(value)
    if key == "gender":
        return {"男性": "男", "male": "男", "女性": "女", "female": "女"}.get(value, value)
    if key == "degree":
        return _DEGREE.get(value, value)
    if key in {"early_internship", "accept_city_assignment", "unified_admission"}:
        return {"true": "是", "false": "否", "yes": "是", "no": "否"}.get(value, value)
    if key == "recruitment_source":
        return {"牛客": "牛客网"}.get(value, value)
    # Never conflate 北京/北京分行, 北大/北京工业大学, or 前30%/前20%.
    return value


def _option_value(field: dict, value: Any, key: str) -> tuple[Any, list[dict], str | None]:
    kind = field["control_kind"]
    if field.get("options_status") not in {"native_complete", "group_complete"}:
        return None, [], "网页选项尚未完整读取，需要先展开并读取真实选项。"
    raw_values = value if isinstance(value, list) else [value]
    if isinstance(value, list) and kind != "checkbox":
        return None, [], "这道题只能单选，但资料中存在多个答案，需要你确认。"
    if not raw_values:
        return None, [], "资料中没有可选答案。"
    chosen = []
    for item in raw_values:
        matches = [option for option in field.get("options", [])
                   if isinstance(option, dict) and option.get("disabled") is not True
                   and option.get("evidence_refs") and option.get("label")
                   and _safe_alias(key, option["label"]) == _safe_alias(key, item)]
        if len(matches) != 1:
            return None, [], "已确认资料与网页可用选项没有唯一对应关系，请从真实选项中确认。"
        selected = matches[0]
        chosen.append({"label": selected["label"], "value": selected.get("value"),
                       "evidence_refs": list(selected["evidence_refs"])})
    labels = [option["label"] for option in chosen]
    return (labels if kind == "checkbox" else labels[0]), chosen, None


def _option_problem(field: dict, key: str) -> str | None:
    options = [item for item in field.get("options", []) if isinstance(item, dict)
               and item.get("disabled") is not True and item.get("label") and item.get("evidence_refs")]
    if not options:
        return "没有读到带证据的可用选项，需要重新检查控件；不能让你填一个不存在的答案。"
    captions = [_safe_alias(key, item["label"]) for item in options]
    if len(set(captions)) != len(captions):
        return "多个网页选项具有相同或等价标题，无法唯一定位，应补读控件而不是让你重复确认。"
    return None


def _date_value(field: dict, value: Any, context: dict, subject: str, semantic_key: str) -> tuple[Any, str | None, list[str]]:
    if not isinstance(value, str):
        return None, "日期资料格式不明确，请确认真实日期。", []
    match = re.fullmatch(r"(\d{4})(?:[-./](\d{1,2}))?(?:[-./](\d{1,2}))?", value.strip())
    if not match:
        return None, "日期不能直接对应网页要求；不会把“至今”替换为今天。", []
    year, month, day = [int(part) if part else None for part in match.groups()]
    if not 1 <= year <= 9999 or month is not None and not 1 <= month <= 12:
        return None, "日期资料不是有效年月，请确认。", []
    if day is not None and not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None, "日期资料不是有效日期，请确认。", []
    precision = _precision(field.get("date_precision"))
    if precision not in {"year", "month", "day"}:
        return None, "尚未确定网页日期控件要求年、月还是日，需要先补读。", []
    policy_refs = []
    if precision == "day" and month is not None and day is None:
        policy = (context.get("policies") or {}).get("month_date_placeholder", {})
        if (policy.get("confirmed") is True and type(policy.get("day")) is int
                and 1 <= policy["day"] <= calendar.monthrange(year, month)[1]
                and policy.get("evidence_ref")
                and semantic_key in policy.get("semantic_keys", [])
                and _scope_allowed(str(policy.get("scope_key", "")), context, subject)):
            day = policy["day"]
            policy_refs.append(str(policy["evidence_ref"]))
        else:
            return None, "资料只有年月，但网页要求具体日期；请补充日期或明确允许使用月份占位日。", []
    if precision in {"month", "day"} and month is None or precision == "day" and day is None:
        return None, "资料的日期精度不足，不能猜测缺失的月份或日期。", []
    formatted = f"{year:04d}"
    if precision in {"month", "day"}:
        formatted += f"-{month:02d}"
    if precision == "day":
        formatted += f"-{day:02d}"
    return formatted, None, policy_refs


def _candidate_refs(candidates: list[dict], field: dict, key: str, subject: str, context: dict) -> list[dict]:
    refs = []
    for item in candidates:
        if item.get("user_id") != context["user_id"] or item.get("entity_id") != subject:
            continue
        if item.get("field_id") != field["id"] and _key(item.get("semantic_key")) != key:
            continue
        if item.get("resume_revision_id") not in (None, context["resume_revision_id"]):
            continue
        if item.get("site_scope") is not None and _site(item["site_scope"]) != _site(context.get("site_scope")):
            continue
        if item.get("chunk_id") and item.get("source_revision_id"):
            refs.append({"chunk_id": item["chunk_id"], "source_revision_id": item["source_revision_id"],
                         "auto_fill_allowed": False})
    return refs[:10]


def validate_confirmation_answer(control_kind: str, options: list[str], value: Any,
                                 date_precision: str | None = None) -> Any:
    """D2 typed confirmation guard; this does not approve a browser action.

    The caller must obtain the kind/options/precision from the immutable owned
    observation, not from the answer request. Returned value is not coerced.
    """
    if control_kind in {"dropdown", "radio", "checkbox"}:
        if (not options or any(not isinstance(item, str) or not item.strip() for item in options)
                or len(set(options)) != len(options)):
            raise ValueError("网页选项为空或有歧义，请重新读取题目")
        if control_kind == "checkbox":
            if (not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value)
                    or len(set(value)) != len(value) or any(item not in options for item in value)):
                raise ValueError("多选答案必须是实际选项中非空、无重复的选择列表")
        elif not isinstance(value, str) or value not in options:
            raise ValueError("答案必须是网页实际提供的一个选项")
    elif control_kind == "text":
        if (not isinstance(value, (str, int, float)) or isinstance(value, bool)
                or isinstance(value, str) and not value.strip()
                or isinstance(value, float) and not math.isfinite(value)):
            raise ValueError("请提供非空文本或有限数值，不能使用列表、对象或布尔值")
        if len(str(value)) > 20000:
            raise ValueError("答案不能超过 20000 字符")
    elif control_kind == "date":
        precision = _precision(date_precision)
        patterns = {"year": r"\d{4}", "month": r"\d{4}-\d{2}", "day": r"\d{4}-\d{2}-\d{2}"}
        if precision not in patterns:
            raise ValueError("网页日期精度尚未确认，请先重新读取日期控件")
        if not isinstance(value, str) or not re.fullmatch(patterns[precision], value):
            raise ValueError("请按网页要求的年、年月或年月日格式提供日期")
        parts = [int(part) for part in value.split("-")]
        try:
            datetime(parts[0], parts[1] if len(parts) > 1 else 1, parts[2] if len(parts) > 2 else 1)
        except ValueError as exc:
            raise ValueError("请提供有效的真实日期") from exc
    else:
        raise ValueError("当前控件尚不支持直接确认答案，需先核实其类型")
    return value


def deduplicate_questions(questions: list[dict]) -> tuple[list[dict], list[dict]]:
    """One confirmation per fact identity, unless observed constraints conflict."""
    groups: dict[tuple, list[dict]] = {}
    for item in questions:
        identity = (item["subject_id"], item["semantic_key"], item["scope_key"])
        groups.setdefault(identity, []).append(item)
    deduplicated, blocked = [], []
    for items in groups.values():
        signatures = {(item["control_kind"], tuple(sorted(item["options"])),
                       item.get("date_precision"), item["expected_fact_revision"]) for item in items}
        if len(signatures) != 1:
            blocked.extend({"field_id": item["field_id"], "label": item["label"],
                "evidence_refs": item["evidence_refs"], "code": "confirmation_scope_conflict",
                "reason": "多道题暂时对应同一资料，但控件或答案范围不同；需要先拆清题意，不能反复让你覆盖答案。"}
                for item in items)
            continue
        merged = dict(items[0])
        merged["field_ids"] = list(dict.fromkeys(item["field_id"] for item in items))
        merged["source_questions"] = list(dict.fromkeys(item["label"] for item in items))
        merged["evidence_refs"] = list(dict.fromkeys(ref for item in items for ref in item["evidence_refs"]))
        deduplicated.append(merged)
    return deduplicated, blocked


def build_answer_plan(recognition: dict, facts: list[dict], context: dict,
                      candidates: list[dict] | None = None) -> dict:
    """Return bounded read-only suggestions and clear, persistable questions.

    ``context`` pins user_id/resume_revision_id/site_scope, contains selected
    resume ``entities`` and optionally a timezone-aware ``now`` for tests.
    Facts require explicit user_id and current revision_id; entity records are
    owned context supplied by the caller, never copied out of the webpage.
    """
    if not context.get("user_id") or not context.get("resume_revision_id") or not _site(context.get("site_scope")):
        raise ValueError("答案规划必须绑定用户、简历版本及招聘站点")
    if recognition.get("schema_version") != "form-recognition-v1":
        raise ValueError("必须先规范化页面识别结果")
    fields = recognition.get("fields", [])
    if not isinstance(fields, list) or len(fields) > MAX_FIELDS or len(facts) > 10000:
        raise ValueError("识别字段或事实数量超过本轮规划上限")
    now = context.get("now") or datetime.now(timezone.utc)
    if isinstance(now, str):
        now = datetime.fromisoformat(now.replace("Z", "+00:00"))
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("规划时间必须包含时区")
    output = {"schema_version": "answer-plan-v1", "read_only": True, "ready_to_execute": False,
              "suggestions": [], "questions": [], "blocked": [], "executable_actions": [],
              "coverage": {"field_count": len(fields), "application_complete": False}}
    id_counts: dict[str, int] = {}
    for item in fields:
        identity = str(item.get("id", ""))
        id_counts[identity] = id_counts.get(identity, 0) + 1
    for field in fields:
        identity, label = str(field.get("id", "")), str(field.get("question", ""))
        base = {"field_id": identity, "label": label, "evidence_refs": list(field.get("evidence_refs") or [])}

        def block(code: str, reason: str) -> None:
            output["blocked"].append({**base, "code": code, "reason": reason})

        if not identity or id_counts[identity] != 1:
            block("duplicate_field", "题目缺少唯一标识或重复，需要重新读取网页。")
            continue
        if field.get("question_status") != "verified" or field.get("matchable") is not True or not base["evidence_refs"]:
            block("observation_required", "网页题干、选项分组或记录边界尚未确认，需要补充观察，不能让用户猜题。")
            continue
        kind = field.get("control_kind")
        if kind == "file" or _FORBIDDEN.search(label):
            block("manual_only", "附件上传、声明或协议需要你单独操作；不会自动同意或上传。")
            continue
        if kind not in {"text", "dropdown", "radio", "checkbox", "date"}:
            block("unsupported_control", "该控件需要进一步识别其操作方式，本轮不会按普通文本填写。")
            continue
        key = _semantic(field)
        if not key:
            block("semantic_unresolved", "题目已读取，但尚不能可靠对应主档案字段；需要核实题意，不会保存模糊映射。")
            continue
        subject, reason = _subject(field, key, context)
        if subject is None:
            block("entity_unresolved", reason)
            continue
        scope = ("resume:" + context["resume_revision_id"] if key in _ENTITY_KEYS else
                 "site:" + _site(context["site_scope"]) if key in _SITE_KEYS else "global")
        scoped = [f for f in facts if f.get("user_id") == context["user_id"] and f.get("subject_id") == subject
                  and _key(f.get("semantic_key")) == key and _scope_allowed(str(f.get("scope_key", "")), context, subject)]
        active = [f for f in scoped if _active(f, now) and f.get("value") not in (None, "", [])]
        retrieval_refs = _candidate_refs(candidates or [], field, key, subject, context)
        record = {**base, "semantic_key": key, "subject_id": subject, "scope_key": scope,
                  "control_kind": kind, "retrieval_refs": retrieval_refs,
                  "date_precision": _precision(field.get("date_precision")) if kind == "date" else None}
        # Only exact target-scope version is used for a future fact update. The
        # presence of a different-scope fact must not cause an optimistic-lock
        # mismatch or silently overwrite that fact.
        target_facts = [f for f in scoped if f.get("scope_key") == scope and f.get("semantic_key") == key]
        expected_revision = max((int(f.get("revision_no", 0)) for f in target_facts), default=0)
        # A current answer confirmed for this exact target scope wins over a
        # broad preference. If that answer was revoked/expired/unconfirmed, do
        # not resurrect an older broader answer as a fallback.
        if target_facts:
            active = [f for f in active if f in target_facts]

        def ask(why: str) -> None:
            options = [o["label"] for o in field.get("options", []) if isinstance(o, dict)
                       and o.get("disabled") is not True and o.get("label") and o.get("evidence_refs")]
            output["questions"].append({**record, "options": options, "reason": why,
                                        "expected_fact_revision": expected_revision})

        if kind in {"dropdown", "radio", "checkbox"} and field.get("options_status") not in {"native_complete", "group_complete"}:
            block("options_incomplete", "尚未完整读到此题的真实选项；先补读，不让你盲填。")
            continue
        if kind in {"dropdown", "radio", "checkbox"} and (option_problem := _option_problem(field, key)):
            block("options_ambiguous", option_problem)
            continue
        if kind == "date" and _precision(field.get("date_precision")) not in {"day", "month", "year"}:
            block("date_precision_unknown", "尚未读清日期格式，需要确认网页要求年、月还是日。")
            continue
        if not active:
            ask("没有适用且已确认的资料，请提供真实答案；检索片段不会直接当作你的确认。")
            continue
        values = {json.dumps(f["value"], ensure_ascii=False, sort_keys=True) for f in active}
        if len(values) != 1:
            ask("现有已确认资料存在冲突，请确认本次使用的答案；不会挑一条猜填。")
            continue
        value = active[0]["value"]
        option_refs, policy_refs = [], []
        if kind in {"dropdown", "radio", "checkbox"}:
            value, option_refs, reason = _option_value(field, value, key)
        elif kind == "date":
            value, reason, policy_refs = _date_value(field, value, context, subject, key)
        else:
            reason = None if isinstance(value, (str, int, float)) and not isinstance(value, bool) else "资料不是单一文本值，需要确认如何表达。"
        if reason:
            ask(reason)
            continue
        try:
            validate_confirmation_answer(kind, [option["label"] for option in field.get("options", [])
                if isinstance(option, dict) and option.get("label") and option.get("disabled") is not True
                and option.get("evidence_refs")], value, field.get("date_precision"))
        except ValueError:
            ask("已确认资料的值不符合这个控件的格式，请核对后补充。")
            continue
        output["suggestions"].append({**record, "value": value, "option_refs": option_refs,
            "policy_refs": policy_refs, "fact_revision_refs": [f["revision_id"] for f in active],
            "auto_fill_allowed": False})
    output["questions"], conflicting = deduplicate_questions(output["questions"])
    output["blocked"].extend(conflicting)
    output["coverage"].update({"suggested": len(output["suggestions"]), "needs_user": len(output["questions"]),
                               "blocked": len(output["blocked"])})
    output["status"] = ("observation_required" if output["blocked"] or recognition.get("needs_observation")
                        else "user_confirmation" if output["questions"] else "review_ready")
    return output
