"""Pure, evidence-addressed recognition for the D2 read-only workflow.

Input is the legacy BrowserSnapshot JSON, not a model-generated form.  An
observation describes the *currently visible document*, never a whole ATS.
This module makes no browser, model, database or network calls.  It does not
turn website text into instructions, infer applicant facts, or issue actions.

The transport must bind the snapshot to an authorized browser observation.
An ``owned`` flag or selector submitted by an arbitrary caller is not itself
an authenticity proof.  Output identifiers are observation-local only.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import re
import unicodedata


SCHEMA_VERSION = "form-recognition-v1"
MAX_FIELDS = 500
MAX_OPTIONS = 600
OWNED_SOURCES = frozenset({
    "explicit", "aria-labelledby", "container-owned", "aria",
    "ant-resume-owned", "autohome-owned", "phoenix-owned",
})
RECORD_SOURCES = frozenset({"ant-resume-owned", "autohome-owned", "phoenix-owned"})
VAGUE = frozenset({
    "是", "否", "男", "女", "保密", "yes", "no", "true", "false",
    "请选择", "请输入", "选择", "select", "please select", "input",
    "个人信息", "基本信息", "教育经历", "其他信息", "上传文件", "选择文件",
})
REASONS = {
    "question_missing": "尚未取得属于此控件的完整题目，不能用选项名称代替题干。",
    "question_unowned": "题目只有附近文字或推测，未证明属于当前控件。",
    "question_conflict": "属于当前控件的题目证据互相矛盾，需要定向核对。",
    "group_unproven": "没有控件组归属证据，不能把同名的选择项合并为一道题。",
    "group_conflict": "选择项的题目、控件类型或记录归属冲突。",
    "group_incomplete": "真实选择项未完整捕获，需要补读这一组控件。",
    "options_missing": "尚未读取真实选项，不能把选择控件当作文本框。",
    "options_partial": "当前只读取到部分选项，不能宣称选项完整。",
    "options_conflict": "同名选项对应多个不同值，不能确定应选哪一个。",
    "options_deferred": "此控件的选项读取尚未完成。",
    "dependent_options": "这是级联选择，当前可见层级不是完整可选路径。",
    "date_precision_missing": "日期控件尚未确定是年月还是年月日。",
    "control_unknown": "尚未确定此控件的真实操作类型。",
    "control_conflict": "控件类型证据互相矛盾，不能直接写入。",
    "control_disabled": "当前控件不可用，不能把已有值当作可写入目标。",
    "readonly_text": "当前文本控件是只读状态，不能按普通输入框填写。",
    "target_missing": "没有唯一控件定位证据。",
    "target_conflict": "同一定位目标重复或被分配给多道题。",
    "record_unresolved": "尚未核实本题所属对象或记录的边界，不能混用不同人员、教育、语言或项目资料。",
    "record_conflict": "一个记录容器出现多条经历或相互冲突的记录锚点。",
    "required_conflict": "必填标记证据冲突，需要核对。",
}


def _text(value: object) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _key(value: object) -> str:
    return unicodedata.normalize("NFKC", _text(value)).strip(" *＊:：").casefold()


def _meaningful(value: object) -> bool:
    text = _key(value)
    return bool(text and text not in VAGUE and not re.match(
        r"^(?:未识别|未命名|字段\s*\d|field\s*\d|input\s*\d)", text, re.I))


def _list(value: object) -> list:
    return value if isinstance(value, list) else []


def _unique(values: list) -> list:
    return list(dict.fromkeys(values))


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _question(raw: dict, ref: str) -> tuple[str, str, list[str], list[str]]:
    candidates = _list(raw.get("question_candidates"))
    owned: dict[str, tuple[str, list[str]]] = {}
    for index, item in enumerate(candidates):
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"))
        if (item.get("owned") is True and item.get("source") in OWNED_SOURCES
                and _meaningful(text)):
            key = _key(text)
            if key not in owned:
                owned[key] = (text.strip(" *＊:："), [])
            owned[key][1].append(f"{ref}/question_candidates/{index}")
    observed = _text(raw.get("question_text") or raw.get("label")).strip(" *＊:：")
    if len(owned) > 1:
        return observed, "ambiguous", [r for _, refs in owned.values() for r in refs], ["question_conflict"]
    if owned:
        key, (text, refs) = next(iter(owned.items()))
        # A weak selected caption such as '是' may be replaced by its unique,
        # owned full question. A contradictory meaningful caption is retained
        # as a conflict; a model must not silently choose one of two questions.
        if _meaningful(observed) and _key(observed) != key:
            return observed, "ambiguous", refs + [f"{ref}/question_text"], ["question_conflict"]
        return text, "verified", refs, []
    if not _meaningful(observed):
        return "", "missing", [], ["question_missing"]
    # Exact-label compatibility for older BrowserSnapshots with no candidate
    # list. Nearby captions, model output and semantic_key never gain trust.
    if not candidates and raw.get("label_source") in OWNED_SOURCES:
        return observed, "verified", [f"{ref}/question_text" if raw.get("question_text") else f"{ref}/label"], []
    return observed, "unverified", [], ["question_unowned"]


def _control(raw: dict) -> tuple[str, list[str]]:
    kind, field_type, role = (_key(raw.get(k)) for k in ("control_kind", "field_type", "role"))
    aliases = {
        "calendar": "date", "month": "date", "date": "date",
        "cascade": "cascader", "cascader": "cascader",
        "native_select": "dropdown", "dropdown": "dropdown",
        "select-one": "dropdown", "select-multiple": "dropdown", "select": "dropdown", "combobox": "dropdown",
        "radio": "radio", "checkbox": "checkbox", "file": "file",
        "text": "text", "textarea": "text", "email": "text", "tel": "text",
        "number": "text", "url": "text", "search": "text", "textbox": "text",
    }
    if kind == "unknown":
        return "unknown", ["control_unknown"]
    claimed = aliases.get(kind, "")
    structural = aliases.get(field_type) or aliases.get(role) or ""
    if raw.get("region_picker") is True:
        if claimed and claimed != "cascader":
            return "unknown", ["control_conflict"]
        claimed = "cascader"
    # A inspected custom combo can be a calendar/cascade. Native inputs of a
    # conflicting family cannot, however, be promoted by a convenient hint.
    compatible = not claimed or not structural or claimed == structural
    if not compatible and claimed in {"date", "cascader"} and field_type == "combobox":
        compatible = bool(_text(raw.get("control_evidence")))
    if not compatible:
        return "unknown", ["control_conflict"]
    selected = claimed or structural
    return (selected, []) if selected else ("unknown", ["control_unknown"])


def _record_question_core(question: str) -> str:
    """Classify the owned question, not explanatory examples inside its note.

    The full question remains unchanged in the observation. Only an explicit
    trailing note delimiter is ignored for subject classification; arbitrary
    parentheses or words within the actual question are not stripped.
    """
    return re.split(r"[（(]\s*(?:注(?:意)?|说明|备注|note\b)", _text(question),
                    maxsplit=1, flags=re.I)[0].strip(" *＊:：")


def _record_kind(raw: dict, question: str) -> str:
    # Subject ownership precedes attribute vocabulary. A parent's degree is
    # not the applicant's education; a student-origin note mentioning schools
    # is not a repeated school record. Semantic hints add caution only when no
    # specific section contradicts them, and never establish a profile binding.
    hint = _text(raw.get("semantic_key")).split(".", 1)[0]
    core = _record_question_core(question)
    sections = _unique([_text(v) for v in reversed(_list(raw.get("section_path")))] +
                       [_text(raw.get("section"))])
    family = r"家庭(?:成员|关系)?|亲属|父亲|母亲|父母|配偶|丈夫|妻子|子女|兄弟|姐妹|family|relatives?|father|mother|parents?|spouse|siblings?"
    if (any(re.search(family, section, re.I) for section in sections) or
            re.match(r"^(?:" + family + r")(?:的|\s|姓名|学历|学位|工作|职业|单位|电话|联系|出生|政治|$)", core, re.I)):
        return "third_party"
    section_kinds = (
        ("education", r"(?:(?:高中|专科|大专|本科|硕士|研究生|博士)\s*)?(?:教育(?:经历|背景|信息|情况)?|学历(?:经历|信息|情况)?|学习经历|education(?:al)?(?: background| history| experience)?|academic(?: background| history| qualifications?))"),
        ("experience", r"实习(?:经历|经验|信息)?|工作(?:经历|经验|信息)?|任职经历|职业经历|学生实践经验|internships?(?: history| experience)?|employment(?: history)?|work experience"),
        ("project", r"项目(?:经历|经验|信息)?|projects?(?: history| experience)?"),
        ("language", r"语言(?:能力|技能|信息)?|外语(?:能力|水平)?|languages?(?: skills?| ability| proficiency)?"),
        ("", r"个人(?:基本)?信息|基本信息|基本资料|个人资料|求职意向|投递意向|应聘信息|其他(?:信息)?|personal(?: information| info| details)?|contact details|general information"),
    )
    for section in sections:
        section = section.strip(" *＊:：")
        # Headings can carry numbering and instructions without changing the
        # record subject (e.g. 教育经历（从高中起填写）). This applies only to
        # structural headings, never to arbitrary question prose.
        section = re.sub(r"^(?:第\s*)?\d+\s*[.、:：]\s*", "", section)
        for kind, pattern in section_kinds:
            suffix = r"(?:\s*(?:\d+|[（(][^（）()]*[）)]))?"
            if re.fullmatch(r"(?:" + pattern + r")" + suffix, section, re.I):
                return kind
    # Full-label anchors only. In particular, an incidental 院校/学历 in a
    # long question must not turn a personal field into an education record.
    if _anchor_key(core, "education"):
        return "education"
    if hint in {"education", "experience", "project", "language"}:
        return hint
    return ""


def _anchor_key(question: str, kind: str) -> str:
    """Question-derived keys only; never let semantic_key invent an anchor."""
    q = _key(question)
    if kind == "education":
        for key, pattern in (
            ("school", r"^(?:毕业|就读|所在)?(?:院校|学校)(?:名称)?$|^(?:university|school)(?: name)?$"),
            ("college", r"^(?:所在|就读)?(?:院系|学院|院系名称|学院名称)$|^(?:college|faculty)(?: name)?$"),
            ("major", r"^(?:所学)?专业(?:名称)?$|^(?:major|subject)(?: name)?$"),
            ("degree", r"^(?:最高)?学历$|^education level$"),
            ("academic_degree", r"^(?:最高)?学位$|^(?:academic )?degree$"),
        ):
            if re.search(pattern, q, re.I):
                return f"education.{key}"
    if kind == "experience" and re.search(r"^(?:实习|工作|任职)?(?:公司|单位)(?:名称)?$|^(?:company|organization)(?: name)?$", q, re.I):
        return "experience.organization"
    if kind == "project" and re.search(r"^项目(?:名称|名)$|^project(?: name| title)?$", q, re.I):
        return "project.name"
    return ""


def _record(raw: dict, question: str) -> dict:
    kind = _record_kind(raw, question)
    key = _text(raw.get("container_key"))
    # Existing ATS record adapters prove applicant experience records only.
    # Their labels cannot establish which relative owns a family-field answer.
    proven = bool(kind != "third_party" and key and not key.startswith("zhida-container-")
                  and raw.get("record_evidence") in RECORD_SOURCES)
    return {"kind": kind, "record_key": key if kind else "",
            "status": "record_observed" if kind and proven else "unresolved" if kind else "not_applicable",
            "anchors": []}


def _options(members: list[dict], kind: str) -> tuple[list[dict], str, list[str]]:
    if kind == "date":
        return [], "calendar", []
    if kind == "cascader":
        return [], "dependent", ["dependent_options"]
    if kind not in {"radio", "checkbox", "dropdown"}:
        return [], "not_applicable", []
    options: dict[tuple[str, str | None], dict] = {}
    reasons: list[str] = []
    for member in members:
        raw, ref = member["raw"], member["ref"]
        for index, value in enumerate(_list(raw.get("options"))):
            if isinstance(value, dict):
                label = _text(value.get("label") or value.get("text"))
                option_value = _text(value.get("value")) if "value" in value else None
                disabled = value.get("disabled") is True
            else:
                label, option_value, disabled = _text(value), None, False
            if not label or _key(label) in {"请选择", "选择", "please select"}:
                continue
            identity = (_key(label), option_value)
            item = options.setdefault(identity, {"label": label, "value": option_value,
                "disabled": disabled, "evidence_refs": []})
            item["disabled"] = item["disabled"] or disabled
            item["evidence_refs"].append(f"{ref}/options/{index}")
    if kind in {"radio", "checkbox"}:
        # Observed member labels/values, not an options array alone, prove that
        # all members of an owned group were captured.
        observed = {_key(m["raw"].get("option_label")) for m in members if _text(m["raw"].get("option_label"))}
        expected = {key[0] for key in options}
        proven_group = all(_text(m["raw"].get("control_group_key")) for m in members)
        if not proven_group and kind == "radio":
            reasons.append("group_unproven")
        if not proven_group and kind == "checkbox" and len(expected) > 1:
            reasons.append("group_unproven")
        for member in members:
            raw, ref = member["raw"], member["ref"]
            label = _text(raw.get("option_label"))
            if not label:
                continue
            value = _text(raw.get("option_value")) or None
            # The member's real value adds to its label evidence without
            # inventing option values for label-only legacy snapshots.
            prior = options.pop((_key(label), None), None)
            item = options.setdefault((_key(label), value), {"label": label, "value": value,
                "disabled": raw.get("disabled") is True, "evidence_refs": []})
            item["disabled"] = item["disabled"] or (prior or {}).get("disabled") is True or raw.get("disabled") is True
            item["evidence_refs"] += (prior or {}).get("evidence_refs", []) + [f"{ref}/option_label"]
        complete = (expected and expected == observed and all(
            m["raw"].get("options_capture") == "group_complete" for m in members) and not reasons)
        if not complete:
            reasons.append("group_incomplete")
        status = "group_complete" if complete else "observed_subset" if options else "unavailable"
    else:
        statuses = {m["raw"].get("options_capture") for m in members}
        native = all(m["raw"].get("field_type") in {"select", "select-one", "select-multiple"} or
                     m["raw"].get("control_kind") == "native_select" for m in members)
        complete = bool(options) and statuses == {"native_complete"} and native
        status = "native_complete" if complete else "deferred" if "deferred" in statuses else "observed_subset" if options else "unavailable"
    labels: dict[str, set] = defaultdict(set)
    for item in options.values():
        labels[_key(item["label"])].add(item["value"])
        item["evidence_refs"] = _unique(item["evidence_refs"])
    if any(len(values) > 1 for values in labels.values()):
        reasons.append("options_conflict")
    if not options or not any(not o["disabled"] for o in options.values()):
        status = "unavailable"
    if status == "unavailable":
        reasons.append("options_missing")
    elif status == "deferred":
        reasons.append("options_deferred")
    elif status == "observed_subset":
        reasons.append("options_partial")
    return list(options.values()), status, _unique(reasons)


def normalize_observation(snapshot: dict) -> dict:
    """Normalize an authenticated observation; incomplete evidence fails closed.

    Raises ValueError on malformed/oversized top-level collections.  Inputs are
    not mutated.  Every actionable-looking result is still a *read-only* fact
    about an observation, not authorization to execute a browser action.
    """
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("fields"), list):
        raise ValueError("A form observation must contain a fields list")
    if len(snapshot["fields"]) > MAX_FIELDS or len(_list(snapshot.get("context_fields"))) > MAX_FIELDS:
        raise ValueError("Form observation field limit exceeded")
    groups: dict[tuple, list[dict]] = defaultdict(list)
    source_rows: list[dict] = []
    pending_sections: list[str] = []
    target_owners: dict[str, list[int]] = defaultdict(list)
    for index, raw in enumerate(snapshot["fields"]):
        if not isinstance(raw, dict) or len(_list(raw.get("options"))) > MAX_OPTIONS:
            raise ValueError("Invalid or oversized field observation")
        if raw.get("field_type") == "section-button" or raw.get("control_kind") == "section":
            pending_sections.append(_text(raw.get("question_text") or raw.get("label")) or "未展开栏目")
            continue
        ref = f"/fields/{index}"
        question, status, refs, reasons = _question(raw, ref)
        kind, control_reasons = _control(raw)
        selector = _text(raw.get("selector"))
        group = _text(raw.get("control_group_key"))
        row = {"raw": raw, "ref": ref, "source_index": index, "question": question,
               "question_status": status, "question_refs": refs, "control_kind": kind,
               "reasons": reasons + control_reasons, "entity": _record(raw, question)}
        source_rows.append(row)
        if selector:
            target_owners[selector].append(index)
        group_key = ("group", group, _text(raw.get("container_key"))) if group and kind in {"radio", "checkbox"} else ("field", index)
        groups[group_key].append(row)

    fields: list[dict] = []
    rows_by_id: dict[str, list[dict]] = {}
    for members in groups.values():
        first = members[0]
        raw = first["raw"]
        digest = hashlib.sha256(json.dumps([(m["source_index"], _text(m["raw"].get("selector")))
            for m in members], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()[:20]
        field_id = "field_" + digest
        reasons = _unique([r for m in members for r in m["reasons"]])
        kinds = {m["control_kind"] for m in members}
        questions = {_key(m["question"]) for m in members if m["question_status"] == "verified"}
        all_questions = {_key(m["question"]) for m in members if _meaningful(m["question"])}
        entities = {(m["entity"]["kind"], m["entity"]["record_key"]) for m in members}
        if len(kinds) > 1 or len(all_questions) > 1 or len(entities) > 1:
            reasons.append("group_conflict")
        kind = first["control_kind"] if len(kinds) == 1 else "unknown"
        disabled = all(m["raw"].get("disabled") is True for m in members)
        if disabled:
            reasons.append("control_disabled")
        if kind == "text" and any(m["raw"].get("readonly") is True for m in members):
            reasons.append("readonly_text")
        question = first["question"]
        question_status = first["question_status"]
        if len(members) > 1 and len(questions) == 1 and len(all_questions) <= 1 and "question_conflict" not in reasons:
            # Proven group ownership permits one group's full question to label
            # the entire group, but never merges unnamed unrelated yes/no items.
            trusted = next(m for m in members if m["question_status"] == "verified")
            question, question_status = trusted["question"], "verified"
            reasons = [r for r in reasons if r not in {"question_missing", "question_unowned"}]
        if "group_conflict" in reasons or "question_conflict" in reasons:
            question_status = "ambiguous"
        options, options_status, option_reasons = _options(members, kind)
        reasons += option_reasons
        precisions = {_text(m["raw"].get("date_precision")) for m in members}
        if kind == "date":
            if len(precisions) != 1 or not precisions.issubset({"date", "month"}):
                reasons.append("date_precision_missing")
        targets = []
        for m in members:
            selector = _text(m["raw"].get("selector"))
            if not selector:
                reasons.append("target_missing")
            elif len(target_owners[selector]) > 1:
                reasons.append("target_conflict")
            targets.append({"selector": selector, "option_label": _text(m["raw"].get("option_label")),
                            "option_value": _text(m["raw"].get("option_value"))})
        entity = dict(first["entity"])
        entity["anchors"] = []
        if entity["status"] == "unresolved":
            reasons.append("record_unresolved")
        required_evidence = _unique([_text(v) for m in members for v in _list(m["raw"].get("required_evidence")) if _text(v)])
        if any(v.startswith("必填证据冲突") for v in required_evidence):
            reasons.append("required_conflict")
        evidence_refs = _unique([r for m in members for r in m["question_refs"]] +
            [f'{m["ref"]}/control_evidence' if m["raw"].get("control_evidence") else f'{m["ref"]}/field_type' for m in members])
        field = {"id": field_id, "question": question, "question_status": question_status,
            "control_kind": kind, "required": any(m["raw"].get("required") is True for m in members),
            "required_status": "required" if any(m["raw"].get("required") is True for m in members) else "not_marked",
            "required_evidence": required_evidence, "options": options, "options_status": options_status,
            "section_path": [_text(v) for v in _list(raw.get("section_path")) if _text(v)],
            "context": _text(raw.get("context")), "help_text": _text(raw.get("help_text")),
            "entity_binding": entity, "semantic_key": _text(raw.get("semantic_key")),
            "evidence_refs": evidence_refs, "matchable": False, "reasons": _unique(reasons),
            "targets": targets, "date_precision": next(iter(precisions)) if len(precisions) == 1 else "",
            "multiple": raw.get("multiple") is True, "accept": _text(raw.get("accept"))}
        field["disabled"] = disabled
        field["readonly"] = raw.get("readonly") is True
        fields.append(field)
        rows_by_id[field_id] = members

    # Record identity is separate from DOM ownership.  Neither array order,
    # entity_scope='education:highest', record_keys nor model hints identify a
    # candidate's degree. Only observed values with owned full questions can
    # become anchors; the matching layer still requires an exact unique match.
    records: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for field in fields:
        entity = field["entity_binding"]
        if entity["status"] == "record_observed":
            records[(entity["kind"], entity["record_key"])].append(field)
    for (kind, record_key), members in records.items():
        anchors, identities = [], defaultdict(set)
        for field in members:
            if field["question_status"] != "verified":
                continue
            anchor_key = _anchor_key(field["question"], kind)
            if not anchor_key:
                continue
            for row in rows_by_id[field["id"]]:
                selector = _text(row["raw"].get("selector"))
                identities[anchor_key].add(selector)
                value = _text(row["raw"].get("current_value"))
                if value and _key(value) not in VAGUE:
                    anchors.append({"field_id": field["id"], "semantic_key": anchor_key,
                        "value": value, "evidence_refs": [*row["question_refs"], f'{row["ref"]}/current_value']})
        # Prompt-only context fields may contribute an anchor, never an action
        # target. Their ownership and exact record boundary are rechecked too.
        for index, raw in enumerate(_list(snapshot.get("context_fields"))):
            if not isinstance(raw, dict) or _text(raw.get("container_key")) != record_key:
                continue
            ref = f"/context_fields/{index}"
            question, status, refs, _ = _question(raw, ref)
            record = _record(raw, question)
            key = _anchor_key(question, kind)
            if status != "verified" or record["status"] != "record_observed" or record["kind"] != kind or not key:
                continue
            selector = _text(raw.get("selector"))
            if selector in identities[key]:
                continue
            identities[key].add(selector)
            value = _text(raw.get("current_value"))
            if value and _key(value) not in VAGUE:
                anchors.append({"field_id": "", "semantic_key": key, "value": value,
                                "evidence_refs": refs + [f"{ref}/current_value"]})
        conflict = any(len(selectors) > 1 for key, selectors in identities.items()
                       if key in {"education.school", "experience.organization", "project.name"})
        values: dict[str, set[str]] = defaultdict(set)
        for anchor in anchors:
            values[anchor["semantic_key"]].add(_key(anchor["value"]))
        conflict = conflict or any(len(v) > 1 for v in values.values())
        for field in members:
            field["entity_binding"]["anchors"] = anchors
            if conflict:
                field["entity_binding"]["status"] = "ambiguous"
                field["reasons"].append("record_conflict")

    issues = []
    for field in fields:
        field["reasons"] = _unique(field["reasons"])
        field["matchable"] = field["question_status"] == "verified" and not field["reasons"]
        issues += [{"code": code, "field_id": field["id"], "detail": REASONS.get(code, code)} for code in field["reasons"]]
    report = snapshot.get("extraction_report")
    report = report if isinstance(report, dict) else {}
    actual = len(source_rows)
    observed = _count(report.get("observed_controls")) if report else actual
    captured = _count(report.get("captured_controls")) if report else actual
    excluded = _count(report.get("intentionally_excluded_controls"))
    unmapped = max(_count(report.get("unmapped_controls")), observed - captured - excluded, 0)
    pending_sections = _unique(pending_sections + [_text(v) for v in _list(report.get("pending_sections")) if _text(v)])
    embedded, shadow = _count(report.get("embedded_regions")), _count(report.get("unread_shadow_regions"))
    limitations = _unique([_text(v) for v in _list(report.get("limitations")) if _text(v)] + [
        "仅覆盖当前可见文档；不能证明条件题、折叠记录、分页或后续步骤已经完整读取。",
        "未标记必填不等于已证明选填；DOM 记录边界不等于已经匹配用户的某条经历。",
    ])
    inconsistent = bool(report and (captured < actual or captured + excluded > observed))
    if inconsistent:
        issues.append({"code": "coverage_inconsistent", "field_id": "", "detail": "控件清单与采集计数不一致，需要重新核对覆盖范围。"})
    partial = bool(unmapped or pending_sections or embedded or shadow or report.get("capture_status") == "partial" or issues)
    status = "partial" if partial else "observed" if report.get("capture_status") == "observed" else "unknown"
    if not fields:
        status = "unknown"
        issues.append({"code": "no_form_fields", "field_id": "", "detail": "本次未读取到可核对的表单字段。"})
    if unmapped:
        issues.append({"code": "unmapped_controls", "field_id": "", "detail": "页面仍存在未映射控件。"})
    if pending_sections:
        issues.append({"code": "pending_sections", "field_id": "", "detail": "仍有未读取或未展开的栏目。"})
    if embedded or shadow:
        issues.append({"code": "unread_regions", "field_id": "", "detail": "嵌入页面或 Shadow DOM 区域尚未读取。"})
    coverage = {"scope": "current_visible_document", "status": status,
        "observed_controls": observed, "captured_controls": captured,
        "intentionally_excluded_controls": excluded, "unmapped_controls": unmapped,
        "question_count": len(fields), "matchable_questions": sum(f["matchable"] for f in fields),
        "pending_sections": pending_sections, "embedded_regions": embedded, "unread_shadow_regions": shadow,
        "limitations": limitations, "application_complete": False}
    return {"schema_version": SCHEMA_VERSION, "fields": fields, "issues": issues,
            "coverage": coverage, "needs_observation": status != "observed",
            "read_only": True, "ready_for_review": False, "executable_actions": []}
