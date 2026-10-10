"""Evidence-quality layer between DOM observation and profile/model mapping.

No network, browser operations or candidate facts. A plausible caption is not
an owned question; visible options are not necessarily a complete dropdown.
Reports describe the current observed document, never an entire application.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict

from .browser_models import (BrowserSnapshot, ExtractionIssue, FieldObservation,
                             FormExtractionReport, PageField)


OWNED_SOURCES = {"explicit", "aria-labelledby", "container-owned", "aria"}
CHOICES = {"radio", "checkbox"}
SELECTS = {"select-one", "select-multiple", "combobox"}
VAGUE = {"是", "否", "男", "女", "yes", "no", "true", "false", "请选择", "请输入",
         "个人信息", "基本信息", "教育经历", "其他信息", "选择"}
MESSAGES = {
    "question_missing": "原题未完整读取，不能根据选项猜题目",
    "question_unowned": "标题来自附近文字，尚未证明它属于当前控件",
    "question_conflict": "同一控件关联到不同题干，需重新核对归属",
    "group_conflict": "选择题成员的题干、选项或记录归属冲突",
    "group_incomplete": "选择题的选项成员未完整捕获，不能把它当完整题目",
    "options_missing": "选择控件的真实选项尚未读到，不能当作自由文本输入",
    "options_partial": "仅观察到当前可见选项，搜索或滚动后可能还有其他选项",
    "options_deferred": "本次未完成选项读取，需定向补读当前控件",
    "dependent_options": "级联或联动选项需逐层读取，不能把第一层当完整答案",
    "date_precision_missing": "已识别日历，但尚未核实年月或年月日格式",
    "record_conflict": "同一记录容器中出现多条同类经历，不能跨记录取值",
    "record_unresolved": "尚未确认网页记录边界，需依靠唯一学历层级或明确经历锚点再绑定资料",
    "control_unknown": "控件类型尚未核实，不能直接写入",
    "required_conflict": "必填标记的观察证据冲突，不能确定为选填，需核对当前题目",
}


def _text(value):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip(" *＊:：")


def _identity(value):
    return _text(value).casefold()


def _meaningful(value):
    text = _text(value)
    return bool(text and text.casefold() not in VAGUE and not re.match(
        r"^(未识别|未命名|字段\s*\d|field\s*\d|input\s*\d)", text, re.I))


def question_key(field):
    # Never merge unrelated questions solely because their captions/names match.
    return (f"{field.field_type}:{field.container_key}:{field.control_group_key}"
            if field.field_type in CHOICES and field.control_group_key else field.selector)


def retain_adapter_evidence(raw, patch, source):
    """A DOM-checked adapter adds evidence; it never erases a contradiction."""
    question = patch.get("question_text", "")
    if question and patch.get("label_source") in OWNED_SOURCES:
        patch["question_candidates"] = [*raw.get("question_candidates", []),
            {"text": question[:500], "source": source, "owned": True}]
    patch["required_evidence"] = list(dict.fromkeys([
        *raw.get("required_evidence", []), *patch.get("required_evidence", []),
    ]))
    if raw.get("required") and patch.get("required") is False:
        # Not seeing a visual marker cannot erase a native required/ARIA
        # marker. Keep both claims visible, rather than declaring it optional.
        patch["required"] = True
        patch["required_evidence"].append(
            "必填证据冲突：" + source + " 未检测到必填标记，原始控件标记为必填")
    elif patch.get("required"):
        patch["required_evidence"].append(source + "：当前控件或题目标签的必填标记")
    patch["required_evidence"] = list(dict.fromkeys(patch["required_evidence"]))
    key = patch.get("container_key", "")
    if (source == "autohome-owned" and key.startswith(("autohome:education:",
            "autohome:experience:", "autohome:project:"))
            or source == "phoenix-owned" and key.startswith("phoenix-record-")):
        patch["record_evidence"] = source
    return patch


def _record_observed(field):
    """A single-question owner is not proof of one repeated resume record.

    These source/key contracts come from DOM-checked adapters or old explicit
    fixtures. Generic scanner-generated zhida-container-* IDs carry no such
    contract, even if their caption happens to imply an education attribute.
    """
    if not field.container_key or field.container_key.startswith("zhida-container-"):
        return False
    if field.record_evidence in {"ant-resume-owned", "autohome-owned", "phoenix-owned"}:
        return True
    kind = field.semantic_key.split(".", 1)[0]
    return (field.container_key.startswith(("ant-resume-", "phoenix-record-"))
            or kind in {"education", "experience", "project"} and
            field.container_key.startswith((f"autohome:{kind}:", f"{kind}:")))


def assess_field(field: PageField) -> FieldObservation:
    issues = []
    question = field.question_text or field.label
    evidence = [item for item in field.question_candidates if item.owned and _meaningful(item.text)]
    identities = {_identity(item.text) for item in evidence}
    if len(identities) > 1:
        status = "ambiguous"
        issues.append("question_conflict")
    elif not _meaningful(question):
        status = "missing"
        issues.append("question_missing")
    elif evidence and _identity(question) in identities:
        status = "verified"
    elif not field.question_candidates and field.label_source in OWNED_SOURCES:
        # Compatibility for exact native/adapter labels; production scanner
        # supplies candidates. Nearby, name and model quotes never gain trust.
        status = "verified"
    else:
        status = "unverified"
        issues.append("question_unowned")
    if field.date_precision or field.control_kind == "calendar":
        options = "calendar"
        if not field.date_precision:
            issues.append("date_precision_missing")
    elif field.region_picker or field.control_kind == "cascade":
        options = "dependent"
        issues.append("dependent_options")
    elif field.field_type in SELECTS | CHOICES:
        captured = field.options_capture
        options = (captured if captured in {"native_complete", "group_complete", "deferred"}
                   else "observed_subset" if field.options else "unavailable")
        if options == "native_complete" and not field.options:
            options = "unavailable"  # an empty/disabled native list is not usable
        if options == "deferred":
            issues.append("options_deferred")
        elif options == "unavailable":
            issues.append("options_missing")
        elif options == "observed_subset":
            issues.append("options_partial")
    else:
        options = "not_applicable"
    repeated = field.semantic_key.startswith(("education.", "experience.", "project.", "language."))
    record = ("container_observed" if _record_observed(field) else "unresolved") if repeated else "not_applicable"
    if record == "unresolved":
        issues.append("record_unresolved")
    if field.control_kind == "unknown":
        issues.append("control_unknown")
    if any(e.startswith("必填证据冲突：") for e in field.required_evidence):
        issues.append("required_conflict")
    return FieldObservation(question_status=status, options_status=options,
        required_status="required" if field.required else "not_marked",
        required_evidence=field.required_evidence,
        record_status=record, issues=issues)


def annotate_fields(fields):
    for field in fields:
        field.observation = assess_field(field)
    groups = defaultdict(list)
    records = defaultdict(list)
    for field in fields:
        groups[question_key(field)].append(field)
        if field.container_key and field.semantic_key.startswith(("education.", "experience.", "project.")):
            records[field.container_key].append(field)
    for members in groups.values():
        if members[0].field_type not in CHOICES:
            continue
        questions = {_identity(f.question_text or f.label) for f in members}
        scopes = {(f.container_key, f.entity_scope) for f in members}
        observed = {_identity(f.option_label) for f in members if f.option_label}
        expected = {_identity(option) for f in members for option in f.options}
        conflict = len(questions) > 1 or len(scopes) > 1
        incomplete = not expected or not expected.issubset(observed)
        for field in members:
            if conflict:
                field.observation.question_status = "ambiguous"
                field.observation.issues.append("group_conflict")
            if incomplete:
                field.observation.options_status = "observed_subset"
                field.observation.issues.append("group_incomplete")
    for members in records.values():
        # Multiple anchors in one DOM record are evidence of an unsafe boundary.
        # A container marker alone is not proof of a single education entry.
        anchors = defaultdict(set)
        for field in members:
            if field.semantic_key in {"education.school", "experience.organization", "project.name"}:
                if field.entity_scope != "education:highest":
                    anchors[field.semantic_key].add(field.selector)
        if any(len(selectors) > 1 for selectors in anchors.values()):
            for field in members:
                field.observation.record_status = "ambiguous"
                field.observation.issues.append("record_conflict")
    return fields


def extraction_block_reason(field):
    observation = field.observation
    if observation is None:
        return ""  # legacy snapshots; production capture always annotates
    blocking = {"question_missing", "question_unowned", "question_conflict", "group_conflict", "group_incomplete",
                "record_conflict", "control_unknown"}
    return "；".join(MESSAGES[issue] for issue in observation.issues if issue in blocking)


def logical_questions(fields):
    groups = defaultdict(list)
    for field in fields:
        if field.field_type != "section-button":
            groups[question_key(field)].append(field)
    result = []
    for key, members in groups.items():
        first = members[0]
        observations = [f.observation or assess_field(f) for f in members]
        issues = list(dict.fromkeys(issue for observation in observations for issue in observation.issues))
        states = {o.question_status for o in observations}
        status = ("ambiguous" if "ambiguous" in states else "missing" if "missing" in states
                  else "unverified" if "unverified" in states else "verified")
        result.append({
            "id": key, "question_text": first.question_text or first.label,
            "question_status": status, "required": any(f.required for f in members),
            "required_status": "required" if any(f.required for f in members) else "not_marked",
            "section_path": first.section_path, "help_text": first.help_text,
            "record_context": {"container_key": first.container_key, "entity_scope": first.entity_scope,
                               "record_keys": first.record_keys,
                               "record_evidence": first.record_evidence,
                               "status": first.observation.record_status if first.observation else "unresolved"},
            "question_evidence": [e.model_dump() for e in first.question_candidates],
            "constraints": first.constraints.model_dump(),
            "required_evidence": list(dict.fromkeys(e for f in members for e in f.required_evidence)),
            "control_kind": first.control_kind or first.field_type,
            "date_precision": first.date_precision,
            "cascade_observation": (first.cascade_observation.model_dump(mode="json")
                                    if first.cascade_observation else None),
            "options_status": observations[0].options_status,
            "options": list(dict.fromkeys(option for f in members for option in f.options)),
            "targets": [{"selector": f.selector, "option_label": f.option_label,
                         "current_value": f.current_value} for f in members],
            "issues": [MESSAGES[issue] for issue in issues],
        })
    return result


def build_report(fields, inventory=None):
    inventory = inventory or {}
    questions = logical_questions(fields)
    actual = sum(f.field_type != "section-button" for f in fields)
    observed = int(inventory.get("observed_controls", actual))
    excluded = int(inventory.get("intentionally_excluded_controls", 0))
    # Pre-adapter counts avoid blaming safe, recognized record refinements for
    # deliberately replacing wrapper/internal targets with one logical target.
    captured = int(inventory.get("captured_controls", actual))
    missing = max(0, observed - excluded - captured)
    frames = int(inventory.get("embedded_regions", 0))
    shadow = int(inventory.get("unread_shadow_regions", 0))
    pending = list(dict.fromkeys(inventory.get("pending_sections", [])))[:30]
    issues = [ExtractionIssue(label=q["question_text"], reason="；".join(q["issues"]),
                              selector=q["targets"][0]["selector"])
              for q in questions if q["issues"]]
    limitations = [
        "范围仅为当前已呈现的文档和已知控件，不代表整张申请表或所有步骤完整",
        "条件题、未展开经历、分页和未挂载的控件需要出现后重新读取",
        "Closed Shadow DOM 的内部控件无法从当前 DOM 枚举；未发现可读区域不等于已证明没有隐藏题",
        "未标记必填不等于已证明选填；下拉可见选项不等于全量选项",
    ]
    if frames:
        limitations.append("存在嵌入区域尚未读取，可能含表单或验证码，不能宣称全页已覆盖")
    if shadow:
        limitations.append("存在 Shadow DOM 控件尚未读取，需要专门的观察适配")
    return FormExtractionReport(capture_status=("partial" if missing or frames or shadow or pending
        else "observed" if inventory else "unknown"), observed_controls=observed,
        captured_controls=captured, intentionally_excluded_controls=excluded, unmapped_controls=missing,
        question_count=len(questions), verified_questions=sum(q["question_status"] == "verified" for q in questions),
        unclear_questions=sum(q["question_status"] != "verified" for q in questions),
        options_pending_questions=sum(q["options_status"] in {"unavailable", "deferred", "observed_subset", "dependent"}
                                      for q in questions),
        ambiguous_record_questions=sum(q["record_context"]["status"] == "ambiguous" for q in questions),
        embedded_regions=frames, unread_shadow_regions=shadow, pending_sections=pending,
        limitations=limitations, issues=issues)


def model_page(snapshot: BrowserSnapshot):
    """Keep action targets distinct from logical questions and prompt context."""
    page = snapshot.model_dump(mode="json")
    action_selectors = {f.selector for f in snapshot.fields}
    groups = {question_key(f) for f in snapshot.fields if f.control_group_key}
    context_members = [f for f in snapshot.context_fields if f.control_group_key and question_key(f) in groups
                       and f.selector not in action_selectors]
    page["questions"] = logical_questions([*snapshot.fields, *context_members])
    for question in page["questions"]:
        for target in question["targets"]:
            target["is_action_target"] = target["selector"] in action_selectors
    page["action_selector_whitelist"] = list(sorted(action_selectors))
    # A filtered mapper page inherits full-document coverage, not a fabricated
    # 'complete' report derived from the smaller set of unresolved fields.
    page["extraction_report"] = (snapshot.extraction_report or build_report(snapshot.fields)).model_dump(mode="json")
    page["context_fields_are_not_action_targets"] = True
    return page


OBSERVED_METADATA = (
    "options", "date_precision", "control_kind", "control_evidence", "help_text",
    "options_capture", "constraints", "required_evidence", "question_candidates",
    "region_picker", "region_value_path",
    "record_evidence",
    "cascade_observation",
)


def merge_observed_metadata(snapshot: BrowserSnapshot, samples, *, refresh=True):
    """Merge scoped read evidence, never selector identity or personal values.

    Region semantics must survive a fresh passive snapshot: the option reader
    can establish a hierarchy without changing the original control_kind.
    Re-annotating the complete set also updates choice-group quality instead
    of copying one member's possibly stale observation summary.

    A filtered mapper view uses refresh=False and inherits its caller's full
    document report; refreshing that view would fabricate subset coverage.
    """
    observed = {field.selector: field for field in samples}
    combined = []
    seen = set()
    for field in [*snapshot.fields, *snapshot.context_fields]:
        sample = observed.get(field.selector)
        if sample is not None:
            evidence = sample.model_copy(deep=True)
            for key in OBSERVED_METADATA:
                # Own each mutable evidence object; a later observation of one
                # view must not silently mutate another view's metadata.
                setattr(field, key, getattr(evidence, key))
        if field.selector not in seen:
            combined.append(field)
            seen.add(field.selector)
    annotate_fields(combined)
    if refresh:
        refresh_report(snapshot)
    return snapshot


def refresh_report(snapshot):
    previous = snapshot.extraction_report
    inventory = previous.model_dump() if previous else None
    # Preserve the original capture scope and surface inventory after a
    # targeted option read; recompute only question/option quality summaries.
    snapshot.extraction_report = build_report(snapshot.fields, inventory)
    if previous and previous.capture_status == "unknown":
        snapshot.extraction_report.capture_status = "unknown"
    return snapshot
