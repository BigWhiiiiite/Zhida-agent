"""Synthetic-only D2 recognition evidence tests; no browser or model used."""
from copy import deepcopy
import json

import pytest

from app.backend_v2.form_recognition import MAX_FIELDS, normalize_observation


def field(question="姓名", **overrides):
    raw = {"selector": "#name", "question_text": question, "label": question,
           "label_source": "explicit", "question_candidates": [
               {"text": question, "source": "explicit", "owned": True}],
           "field_type": "text", "control_kind": "text", "control_evidence": "HTML text",
           "options": [], "options_capture": "", "required": True,
           "required_evidence": ["HTML required"]}
    raw.update(overrides)
    return raw


def snapshot(*fields, **overrides):
    result = {"session_id": "synthetic-session", "url": "https://jobs.example.invalid/form",
              "title": "Synthetic application", "fields": list(fields), "context_fields": [],
              "extraction_report": {"scope": "current_visible_document", "capture_status": "observed",
                "observed_controls": len(fields), "captured_controls": len(fields),
                "unmapped_controls": 0, "pending_sections": [], "embedded_regions": 0,
                "unread_shadow_regions": 0}}
    result.update(overrides)
    return result


def choice(option, question="除上述选择外，是否还接受其他城市分配", **overrides):
    raw = field(question, selector=f"#choice-{option}", field_type="radio", control_kind="radio",
                control_evidence="HTML radio", control_group_key="dom-group-1", option_label=option,
                option_value={"是": "yes", "否": "no"}.get(option, option),
                options=["是", "否"], options_capture="group_complete")
    raw.update(overrides)
    return raw


def education(question="学院名称", key="record-master", **overrides):
    raw = field(question, selector=f"#{key}-{question}", container_key=key,
                record_evidence="ant-resume-owned", section_path=["教育经历"],
                semantic_key="education.college", entity_scope="education:master")
    raw.update(overrides)
    return raw


def test_verified_text_stays_scoped_and_never_executable():
    raw = snapshot(field(current_value="真实姓名不应进入普通字段结果"))
    before = deepcopy(raw)
    result = normalize_observation(raw)
    assert result["schema_version"] == "form-recognition-v1"
    assert result["fields"][0]["question"] == "姓名"
    assert result["fields"][0]["matchable"]
    assert result["fields"][0]["evidence_refs"] == ["/fields/0/question_candidates/0", "/fields/0/control_evidence"]
    assert result["coverage"]["status"] == "observed"
    assert result["coverage"]["scope"] == "current_visible_document"
    assert result["coverage"]["application_complete"] is False
    assert result["ready_for_review"] is False
    assert result["executable_actions"] == []
    assert "真实姓名" not in json.dumps(result, ensure_ascii=False)
    assert raw == before
    assert normalize_observation(raw) == result


@pytest.mark.parametrize("hint", ["candidate.student_origin", "education.school"])
def test_student_origin_school_note_cannot_become_education_record(hint):
    question = ("生源地（注：国内院校学生为参加高考所在地，如上高中时转学至其他省份，"
                "仍填写户口所在地，未参加高考的留学生为现户口所在地）")
    row = normalize_observation(snapshot(field(question, section_path=["个人基本信息"],
        semantic_key=hint)))["fields"][0]
    assert row["question"] == question  # Never shorten the question shown to the user.
    assert row["entity_binding"]["kind"] == ""
    assert row["entity_binding"]["status"] == "not_applicable"
    assert "record_unresolved" not in row["reasons"]


@pytest.mark.parametrize("question", [
    "生源地（注：国内院校学生为参加高考所在地）",
    "生源地(说明：学校所在地不一定是高考所在地)",
    "是否接受学校以外的工作地点", "是否获得双学位", "是否参加学历认证",
])
def test_education_word_in_non_record_question_is_not_an_education_anchor(question):
    row = normalize_observation(snapshot(field(question)))["fields"][0]
    assert row["entity_binding"]["status"] == "not_applicable"
    assert row["question"] == question


@pytest.mark.parametrize("question,section", [
    ("父亲学历", "家庭成员及社会关系"), ("母亲学历", "家庭成员及社会关系"),
    ("父亲学历", "个人基本信息"), ("母亲学位", "教育经历"),
    ("学历", "家庭成员及社会关系"), ("出生日期", "家庭成员及社会关系"),
    ("姓名", "亲属信息"), ("degree", "family information"),
])
def test_relative_attribute_is_not_applicant_education_or_person(question, section):
    raw = field(question, section_path=[section], semantic_key="education.degree",
                container_key="claimed-applicant-record", record_evidence="ant-resume-owned",
                current_value="不能成为本人经历锚点")
    row = normalize_observation(snapshot(raw))["fields"][0]
    assert row["entity_binding"]["kind"] == "third_party"
    assert row["entity_binding"]["status"] == "unresolved"
    assert row["entity_binding"]["anchors"] == []
    assert not row["matchable"]


@pytest.mark.parametrize("question,section,kind", [
    ("得分", "语言能力", "language"), ("其他说明", "语言能力", "language"),
    ("项目描述", "项目经历", "project"), ("实习内容", "实习经历", "experience"),
])
def test_non_education_record_gaps_keep_own_kind_and_generic_message(question, section, kind):
    result = normalize_observation(snapshot(field(question, section_path=[section],
        semantic_key="education.degree")))
    row = result["fields"][0]
    assert row["entity_binding"]["kind"] == kind
    assert row["entity_binding"]["status"] == "unresolved"
    issue = next(issue for issue in result["issues"] if issue["code"] == "record_unresolved")
    assert "本科" not in issue["detail"] and "硕士" not in issue["detail"]
    assert not row["matchable"]


@pytest.mark.parametrize("outer_section", ["", "个人基本信息"])
def test_nearest_education_section_wins_over_outer_personal_heading(outer_section):
    row = normalize_observation(snapshot(education(section_path=["个人基本信息", "教育经历", "第1条"])))["fields"][0]
    assert row["entity_binding"]["kind"] == "education"
    assert row["entity_binding"]["status"] == "record_observed"
    raw = education(section=outer_section, section_path=["个人基本信息", "教育经历", "第1条"])
    assert normalize_observation(snapshot(raw))["fields"][0]["entity_binding"]["kind"] == "education"


@pytest.mark.parametrize("section", ["教育经历（硕士）", "2. 教育背景（从高中起填写）",
                                      "Education history (Required)", "本科教育经历（1）"])
def test_education_heading_notes_do_not_remove_record_boundary_requirement(section):
    row = normalize_observation(snapshot(field("出生日期", section_path=[section])))["fields"][0]
    assert row["entity_binding"]["kind"] == "education"
    assert row["entity_binding"]["status"] == "unresolved"
    assert not row["matchable"]


@pytest.mark.parametrize("question", ["学校名称", "学院名称", "所学专业", "学历", "学位"])
def test_actual_education_question_still_requires_a_proven_record(question):
    row = normalize_observation(snapshot(field(question, section_path=["教育经历"])))["fields"][0]
    assert row["entity_binding"]["kind"] == "education"
    assert row["entity_binding"]["status"] == "unresolved"
    assert "record_unresolved" in row["reasons"]
    assert not row["matchable"]


def test_radio_group_uses_owned_full_question_not_yes_no_labels():
    yes = choice("是", question_text="是", label="是")
    no = choice("否", question_text="否", label="否")
    result = normalize_observation(snapshot(yes, no))
    assert len(result["fields"]) == 1
    row = result["fields"][0]
    assert row["question"] == "除上述选择外，是否还接受其他城市分配"
    assert row["matchable"]
    assert row["options_status"] == "group_complete"
    assert [(o["label"], o["value"]) for o in row["options"]] == [("是", "yes"), ("否", "no")]
    assert len(row["targets"]) == 2


def test_one_owned_group_question_can_cover_members_but_not_unproven_groups():
    yes = choice("是")
    no = choice("否", question_text="否", label="否", question_candidates=[])
    assert normalize_observation(snapshot(yes, no))["fields"][0]["matchable"]
    yes["control_group_key"] = no["control_group_key"] = ""
    result = normalize_observation(snapshot(yes, no))
    assert len(result["fields"]) == 2
    assert all(not f["matchable"] for f in result["fields"])
    assert all("group_unproven" in f["reasons"] for f in result["fields"])


def test_same_name_and_same_question_do_not_merge_unrelated_radios():
    yes = choice("是", control_group_key="", name="shared")
    no = choice("否", control_group_key="", name="shared")
    result = normalize_observation(snapshot(yes, no))
    assert len(result["fields"]) == 2
    assert all(not f["matchable"] for f in result["fields"])


def test_group_with_conflicting_questions_fails_closed():
    result = normalize_observation(snapshot(choice("是"), choice("否", question="是否可提前实习")))
    row = result["fields"][0]
    assert row["question_status"] == "ambiguous"
    assert "group_conflict" in row["reasons"]
    assert not row["matchable"]


def test_group_membership_requires_actual_options_not_options_array_only():
    row = normalize_observation(snapshot(choice("是")))["fields"][0]
    assert "group_incomplete" in row["reasons"]
    assert row["options_status"] == "observed_subset"
    assert not row["matchable"]


def test_empty_yes_no_caption_is_not_question_even_if_marked_verified():
    raw = choice("是", question="是", control_group_key="")
    raw["observation"] = {"question_status": "verified", "options_status": "group_complete"}
    row = normalize_observation(snapshot(raw))["fields"][0]
    assert row["question"] == ""
    assert row["question_status"] == "missing"
    assert not row["matchable"]


@pytest.mark.parametrize("source", ["nearby", "name", "placeholder", "model", "unknown", "visual_guess"])
def test_plausible_caption_or_model_claim_is_not_owned_evidence(source):
    raw = field("期望工作城市", label_source=source,
                question_candidates=[{"text": "期望工作城市", "source": source, "owned": True}])
    row = normalize_observation(snapshot(raw))["fields"][0]
    assert row["question_status"] == "unverified"
    assert "question_unowned" in row["reasons"]


def test_conflicting_owned_evidence_is_not_silently_overwritten():
    raw = field("紧急联系人姓名", question_candidates=[
        {"text": "紧急联系人姓名", "source": "explicit", "owned": True},
        {"text": "本人姓名", "source": "aria", "owned": True},
    ])
    row = normalize_observation(snapshot(raw))["fields"][0]
    assert row["question_status"] == "ambiguous"
    assert "question_conflict" in row["reasons"]
    assert len([r for r in row["evidence_refs"] if "question_candidates" in r]) == 2


def test_meaningful_selected_title_conflict_is_preserved():
    raw = field("现居住地", question_text="籍贯")
    row = normalize_observation(snapshot(raw))["fields"][0]
    assert row["question_status"] == "ambiguous"
    assert not row["matchable"]


def test_old_exact_native_label_supported_but_missing_ownership_not():
    raw = field(question_candidates=[])
    assert normalize_observation(snapshot(raw))["fields"][0]["matchable"]
    raw["label_source"] = "nearby"
    assert not normalize_observation(snapshot(raw))["fields"][0]["matchable"]


def test_native_dropdown_preserves_label_only_options_without_fabricating_values():
    raw = field("期望工作城市", field_type="select-one", control_kind="native_select",
                options=["请选择", "北京", "上海"], options_capture="native_complete")
    row = normalize_observation(snapshot(raw))["fields"][0]
    assert row["control_kind"] == "dropdown"
    assert row["options_status"] == "native_complete"
    assert row["matchable"]
    assert [(o["label"], o["value"]) for o in row["options"]] == [("北京", None), ("上海", None)]


@pytest.mark.parametrize("capture,options,status,reason", [
    ("", [], "unavailable", "options_missing"),
    ("observed_subset", ["北京"], "observed_subset", "options_partial"),
    ("native_complete", ["北京"], "observed_subset", "options_partial"),
    ("deferred", [], "unavailable", "options_missing"),
    ("deferred", ["北京"], "deferred", "options_deferred"),
])
def test_custom_dropdown_never_becomes_text_or_claims_complete(capture, options, status, reason):
    raw = field("当前所处地", field_type="combobox", control_kind="dropdown",
                options_capture=capture, options=options)
    row = normalize_observation(snapshot(raw))["fields"][0]
    assert row["control_kind"] == "dropdown"
    assert row["options_status"] == status
    assert reason in row["reasons"]
    assert not row["matchable"]


def test_disabled_options_are_not_available_answers():
    raw = field("面试城市", field_type="select-one", control_kind="native_select",
                options_capture="native_complete", options=[{"label": "远程面试", "value": "remote", "disabled": True}])
    row = normalize_observation(snapshot(raw))["fields"][0]
    assert row["options"][0]["disabled"]
    assert row["options_status"] == "unavailable"
    assert not row["matchable"]


def test_disabled_radio_option_overlay_does_not_erase_existing_disabled_evidence():
    yes, no = choice("是"), choice("否")
    yes["options"] = [{"label": "是", "disabled": True}, {"label": "否", "disabled": False}]
    raw = snapshot(yes, no)
    _, recognized, plan = _pipeline(raw, [_fact("accept_city_assignment", "是")])
    options = {o["label"]: o for o in recognized["fields"][0]["options"]}
    assert options["是"]["disabled"] is True
    assert options["否"]["disabled"] is False
    assert plan["suggestions"] == []
    assert plan["questions"][0]["options"] == ["否"]


@pytest.mark.parametrize("extra,reason", [({"disabled": True}, "control_disabled"),
                                         ({"readonly": True}, "readonly_text")])
def test_disabled_or_readonly_text_cannot_become_a_write_target(extra, reason):
    _, recognized, plan = _pipeline(snapshot(field(**extra)), [_fact("name", "合成同学")])
    assert reason in recognized["fields"][0]["reasons"]
    assert recognized["fields"][0]["matchable"] is False
    assert plan["suggestions"] == [] and plan["questions"] == []
    assert plan["blocked"][0]["code"] == "observation_required"


def test_readonly_native_dropdown_and_calendar_remain_structured_controls():
    dropdown = field("期望工作城市", field_type="select-one", control_kind="native_select", readonly=True,
                     options=["北京"], options_capture="native_complete")
    date = field("出生日期", selector="#birth", field_type="date", control_kind="date", readonly=True,
                 date_precision="date")
    _, recognized, plan = _pipeline(snapshot(dropdown, date), [
        _fact("desired_work_city", "北京"), _fact("birth_date", "2000-01-02")])
    assert all(f["matchable"] for f in recognized["fields"])
    assert [s["value"] for s in plan["suggestions"]] == ["北京", "2000-01-02"]


def test_same_label_different_dom_values_requires_disambiguation():
    raw = field("工作城市", field_type="select-one", control_kind="native_select", options_capture="native_complete",
                options=[{"label": "北京", "value": "1"}, {"label": "北京", "value": "2"}])
    row = normalize_observation(snapshot(raw))["fields"][0]
    assert "options_conflict" in row["reasons"]
    assert not row["matchable"]


@pytest.mark.parametrize("kind,field_type,precision,expected", [
    ("calendar", "combobox", "month", "date"),
    ("date", "date", "date", "date"),
    ("month", "month", "month", "date"),
    ("file", "file", "", "file"),
    ("cascade", "combobox", "", "cascader"),
])
def test_actual_control_families_remain_distinct(kind, field_type, precision, expected):
    raw = field("测试问题", control_kind=kind, field_type=field_type, date_precision=precision)
    row = normalize_observation(snapshot(raw))["fields"][0]
    assert row["control_kind"] == expected
    if expected == "cascader":
        assert row["options_status"] == "dependent"
        assert "dependent_options" in row["reasons"]
        assert not row["matchable"]
    else:
        assert row["matchable"]


def test_calendar_without_precision_does_not_guess_first_day():
    raw = field("开始时间", field_type="combobox", control_kind="calendar")
    row = normalize_observation(snapshot(raw))["fields"][0]
    assert "date_precision_missing" in row["reasons"]
    assert not row["matchable"]


def test_control_conflict_and_unknown_never_default_to_text():
    for raw in (field(control_kind="unknown"), field(field_type="radio", control_kind="text"),
                field(field_type="custom-widget", control_kind="")):
        row = normalize_observation(snapshot(raw))["fields"][0]
        assert row["control_kind"] == "unknown"
        assert not row["matchable"]


def test_missing_or_duplicate_target_is_blocked():
    empty = normalize_observation(snapshot(field(selector="")))["fields"][0]
    assert "target_missing" in empty["reasons"]
    duplicate = normalize_observation(snapshot(field(), field()))
    assert all("target_conflict" in f["reasons"] for f in duplicate["fields"])
    assert all(not f["matchable"] for f in duplicate["fields"])


def test_education_record_and_resume_identity_are_separate():
    row = normalize_observation(snapshot(education()))["fields"][0]
    assert row["entity_binding"] == {"kind": "education", "record_key": "record-master",
                                     "status": "record_observed", "anchors": []}
    assert "education:master" not in json.dumps(row)
    assert row["matchable"]  # Answer planner must still resolve a unique profile entity.


def test_generic_container_does_not_prove_single_education_record():
    for extra in ({"record_evidence": ""}, {"container_key": "zhida-container-123"}, {"record_evidence": "model"}):
        row = normalize_observation(snapshot(education(**extra)))["fields"][0]
        assert row["entity_binding"]["status"] == "unresolved"
        assert "record_unresolved" in row["reasons"]
        assert not row["matchable"]


def test_education_anchor_is_question_derived_not_semantic_hint():
    school = education("学校名称", semantic_key="education.college", current_value="帝国理工学院")
    college = education()
    result = normalize_observation(snapshot(school, college))
    anchors = result["fields"][1]["entity_binding"]["anchors"]
    assert anchors[0]["semantic_key"] == "education.school"
    assert anchors[0]["value"] == "帝国理工学院"
    assert anchors[0]["evidence_refs"] == ["/fields/0/question_candidates/0", "/fields/0/current_value"]
    invented = education("随便问题", semantic_key="education.school", current_value="不能作为学校")
    assert normalize_observation(snapshot(invented))["fields"][0]["entity_binding"]["anchors"] == []


def test_bachelor_and_master_colleges_do_not_share_record_anchors():
    master = education("学校名称", key="master", current_value="帝国理工学院")
    bachelor = education("学校名称", key="bachelor", current_value="北京工业大学", entity_scope="education:master")
    result = normalize_observation(snapshot(master, education(key="master"), bachelor, education(key="bachelor")))
    assert {a["value"] for a in result["fields"][1]["entity_binding"]["anchors"]} == {"帝国理工学院"}
    assert {a["value"] for a in result["fields"][3]["entity_binding"]["anchors"]} == {"北京工业大学"}


def test_two_school_controls_in_one_record_block_even_if_scope_claims_highest():
    one = education("学校名称", selector="#school1", current_value="帝国理工学院", entity_scope="education:highest")
    two = education("学校名称", selector="#school2", current_value="北京工业大学", entity_scope="education:highest")
    result = normalize_observation(snapshot(one, two, education()))
    assert all(f["entity_binding"]["status"] == "ambiguous" for f in result["fields"])
    assert all("record_conflict" in f["reasons"] and not f["matchable"] for f in result["fields"])


def test_context_fields_are_anchors_only_never_fill_targets():
    school = education("学校名称", selector="#context-school", current_value="帝国理工学院")
    result = normalize_observation(snapshot(education(), context_fields=[school]))
    assert len(result["fields"]) == 1
    row = result["fields"][0]
    assert row["entity_binding"]["anchors"][0]["value"] == "帝国理工学院"
    assert row["entity_binding"]["anchors"][0]["evidence_refs"][-1] == "/context_fields/0/current_value"
    assert "#context-school" not in json.dumps(row["targets"])


def test_required_conflict_does_not_turn_field_optional():
    raw = field(required_evidence=["HTML required", "必填证据冲突：适配器未发现星号"])
    row = normalize_observation(snapshot(raw))["fields"][0]
    assert row["required"]
    assert "required_conflict" in row["reasons"]
    assert not row["matchable"]


def test_coverage_detects_iframe_shadow_unmapped_and_pending_sections():
    report = {"capture_status": "observed", "observed_controls": 4, "captured_controls": 1,
              "intentionally_excluded_controls": 1, "embedded_regions": 1, "unread_shadow_regions": 2,
              "pending_sections": ["实习经历"]}
    result = normalize_observation(snapshot(field(), extraction_report=report))
    assert result["coverage"]["status"] == "partial"
    assert result["coverage"]["unmapped_controls"] == 2
    assert result["coverage"]["pending_sections"] == ["实习经历"]
    assert result["needs_observation"]
    assert {i["code"] for i in result["issues"]} >= {"pending_sections", "unread_regions", "unmapped_controls"}
    assert result["coverage"]["application_complete"] is False


def test_expandable_sections_do_not_become_text_fields():
    expander = field("添加教育经历", field_type="section-button", control_kind="section")
    result = normalize_observation(snapshot(field(), expander))
    assert len(result["fields"]) == 1
    assert result["coverage"]["pending_sections"] == ["添加教育经历"]
    assert result["needs_observation"]


def test_missing_report_empty_capture_or_injected_full_completion_not_ready():
    no_report = normalize_observation({"fields": [field()]})
    assert no_report["coverage"]["status"] == "unknown"
    assert no_report["needs_observation"]
    empty = normalize_observation(snapshot())
    assert empty["coverage"]["status"] == "unknown"
    assert any(i["code"] == "no_form_fields" for i in empty["issues"])
    raw = snapshot(field(), application_complete=True, ready_for_review=True,
                   executable_actions=[{"action": "submit"}])
    result = normalize_observation(raw)
    assert result["coverage"]["application_complete"] is False
    assert result["ready_for_review"] is False
    assert result["executable_actions"] == []


def test_invalid_or_oversized_input_fails_without_partial_silent_drop():
    for raw in ({}, {"fields": "bad"}, {"fields": [None]}, {"fields": [field()] * (MAX_FIELDS + 1)}):
        with pytest.raises(ValueError):
            normalize_observation(raw)


def test_inconsistent_coverage_counts_cannot_claim_visible_document_complete():
    result = normalize_observation(snapshot(field(), extraction_report={
        "capture_status": "observed", "captured_controls": 0, "observed_controls": 0}))
    assert result["needs_observation"]
    assert result["coverage"]["status"] == "partial"
    assert any(i["code"] == "coverage_inconsistent" for i in result["issues"])


def _pipeline(raw, facts=(), *, entities=()):
    """Exercise the real scanner Pydantic schema and both D2 boundaries.

    This is an offline contract test, not a claim that the DOM collector has
    run on a real employer website. The website artifacts here are synthetic.
    """
    from app.backend_v2.observation_adapter import adapt_browser_snapshot
    from app.backend_v2.answer_planning import build_answer_plan

    # The adapter validates BrowserSnapshot internally, while preserving the
    # explicit D2 disabled/typed-option extensions that the old model lacks.
    adapted = adapt_browser_snapshot(raw)
    recognized = normalize_observation(adapted)
    plan = build_answer_plan(recognized, list(facts), {
        "user_id": "synthetic-user", "resume_revision_id": "synthetic-resume-v1",
        "site_scope": "jobs.example.invalid", "now": "2026-10-12T00:00:00+00:00",
        "entities": list(entities),
    })
    assert plan["executable_actions"] == [] and plan["ready_to_execute"] is False
    return adapted, recognized, plan


def _fact(key, value, subject="person", **extra):
    return {"user_id": "synthetic-user", "semantic_key": key, "value": value,
            "subject_id": subject, "scope_key": "global", "confirmation": "confirmed",
            "revision_id": "synthetic-fact-" + key + subject, "revision_no": 1, **extra}


@pytest.mark.parametrize("question,key,value", [
    ("姓名", "name", "合成本人姓名"), ("出生日期", "birth_date", "2000-01-01"),
    ("学历", "degree", "硕士"), ("政治面貌", "political_status", "合成政治面貌"),
])
def test_third_party_classification_cannot_fall_back_to_confirmed_person_fact(question, key, value):
    raw = snapshot(field(question, section_path=["家庭成员及社会关系"],
        container_key="fake-education-record", record_evidence="ant-resume-owned",
        semantic_key="education.degree"))
    _, recognized, plan = _pipeline(raw, [_fact(key, value)])
    assert recognized["fields"][0]["entity_binding"]["kind"] == "third_party"
    assert plan["suggestions"] == [] and plan["questions"] == []
    assert plan["blocked"][0]["code"] == "observation_required"


def test_known_emergency_contact_question_is_not_reclassified_as_family_record():
    _, recognized, plan = _pipeline(snapshot(field("紧急联系人姓名", section_path=["个人基本信息"])),
        [_fact("emergency_contact_name", "合成紧急联系人")])
    assert recognized["fields"][0]["entity_binding"]["status"] == "not_applicable"
    assert plan["suggestions"][0]["semantic_key"] == "emergency_contact_name"
    assert plan["suggestions"][0]["value"] == "合成紧急联系人"


def test_browser_model_radio_contract_yields_one_evidenced_answer_not_two_questions():
    raw = snapshot(choice("是"), choice("否"))
    _, recognized, plan = _pipeline(raw, [_fact("accept_city_assignment", "是")])
    assert len(recognized["fields"]) == 1
    assert len(plan["suggestions"]) == 1
    assert plan["suggestions"][0]["label"] == "除上述选择外，是否还接受其他城市分配"
    assert plan["suggestions"][0]["value"] == "是"
    assert plan["suggestions"][0]["option_refs"][0]["value"] == "yes"
    assert not plan["questions"]


def test_browser_model_education_context_anchor_binds_correct_college():
    anchor = education("学校名称", semantic_key="education.school", current_value="合成硕士大学")
    raw = snapshot(education(), context_fields=[anchor])
    entities = [{"entity_id": "synthetic-master", "entity_type": "education",
                 "education_level": "硕士", "content": {"school": "合成硕士大学"}},
                {"entity_id": "synthetic-bachelor", "entity_type": "education",
                 "education_level": "本科", "content": {"school": "合成本科大学"}}]
    _, recognized, plan = _pipeline(raw, [_fact("college", "合成硕士学院", "synthetic-master"),
                                         _fact("college", "合成本科学院", "synthetic-bachelor")], entities=entities)
    assert len(recognized["fields"]) == 1
    assert plan["suggestions"][0]["value"] == "合成硕士学院"
    assert plan["suggestions"][0]["subject_id"] == "synthetic-master"
    assert "#record-master-学校名称" not in json.dumps(recognized["fields"][0]["targets"], ensure_ascii=False)


def test_browser_model_unowned_context_anchor_is_stripped_then_blocked_not_guessed():
    anchor = education("学校名称", semantic_key="education.school", current_value="不能使用的错误大学",
                       question_candidates=[{"text": "学校名称", "source": "nearby", "owned": False}])
    raw = snapshot(education(), context_fields=[anchor])
    adapted, recognized, plan = _pipeline(raw, [_fact("college", "不应被用到的学院", "claimed-master")], entities=[
        {"entity_id": "claimed-master", "entity_type": "education", "content": {"school": "不能使用的错误大学"}}])
    assert "current_value" not in adapted["context_fields"][0]
    assert recognized["fields"][0]["entity_binding"]["anchors"] == []
    assert plan["suggestions"] == [] and plan["questions"] == []
    assert plan["blocked"][0]["code"] == "entity_unresolved"


def test_same_radio_group_key_in_two_education_records_never_cross_merges():
    controls = []
    for record in ("master", "bachelor"):
        for option in ("是", "否"):
            controls.append(choice(option, question="是否统招", selector=f"#{record}-{option}",
                container_key=record, record_evidence="ant-resume-owned",
                section_path=["教育经历"], semantic_key="education.unified_admission"))
    _, recognized, plan = _pipeline(snapshot(*controls))
    assert len(recognized["fields"]) == 2
    assert {f["entity_binding"]["record_key"] for f in recognized["fields"]} == {"master", "bachelor"}
    assert all(len(f["targets"]) == 2 for f in recognized["fields"])
    # No profile identity from record names or repeated array order.
    assert all(item["code"] == "entity_unresolved" for item in plan["blocked"])
    assert plan["suggestions"] == []


def test_browser_model_date_precision_matches_actual_legacy_date_enum():
    raw = snapshot(field("出生日期", field_type="date", control_kind="date", date_precision="date"))
    _, recognized, plan = _pipeline(raw, [_fact("birth_date", "2000-01-02")])
    assert recognized["fields"][0]["date_precision"] == "date"
    assert plan["suggestions"][0]["value"] == "2000-01-02"
    assert not plan["questions"] and not plan["blocked"]


def test_browser_model_unknown_sections_remain_system_work_not_user_questions():
    raw = snapshot(field())
    raw["extraction_report"].update({"pending_sections": ["尚未读取的家庭关系栏目"], "capture_status": "partial"})
    _, recognized, plan = _pipeline(raw, [_fact("name", "合成同学")])
    assert recognized["needs_observation"]
    assert plan["status"] == "observation_required"
    assert plan["suggestions"][0]["value"] == "合成同学"
    assert plan["questions"] == []


def test_existing_autohome_fixture_select2_and_record_shapes_fail_closed_without_unseen_evidence():
    """Source the exact labels/options from the repository's anonymous HTML.

    PageField metadata here represents the existing collector boundary, not a
    second HTML scraper: native SELECT+visible Select2 is ONE logical target,
    two blank education records cannot be assigned by their ordinal numbers,
    and a user-only declaration is never made executable.
    """
    from pathlib import Path
    from html.parser import HTMLParser

    class SelectFixture(HTMLParser):
        def __init__(self):
            super().__init__()
            self.selects, self.current, self.in_option = [], None, False
        def handle_starttag(self, tag, attrs):
            if tag == "select":
                self.current = []
                self.selects.append(self.current)
            elif tag == "option" and self.current is not None:
                self.in_option = True
        def handle_endtag(self, tag):
            if tag == "option":
                self.in_option = False
            elif tag == "select":
                self.current = None
        def handle_data(self, data):
            if self.in_option and self.current is not None and data.strip() and data.strip() != "请选择":
                self.current.append(data.strip())

    html = (Path(__file__).parents[1] / "fixtures" / "autohome_delivery_form.html").read_text()
    parser = SelectFixture()
    parser.feed(html)
    assert len(parser.selects) == 5  # Mirrors fixture's documented 5 logical selects.
    gender_options = next(options for options in parser.selects if options == ["男", "女"])
    raw = snapshot(
        field("性别", selector="#select2-sex-container", field_type="combobox", control_kind="dropdown",
              options=gender_options, options_capture="observed_subset"),
        education("开始时间", key="autohome:education:1", record_evidence="autohome-owned", semantic_key="education.start_date"),
        education("开始时间", key="autohome:education:2", record_evidence="autohome-owned", semantic_key="education.start_date"),
        field("简历附件", selector="#uploadFile", field_type="file", control_kind="file"),
    )
    _, recognized, plan = _pipeline(raw, [_fact("gender", "男")])
    assert len(recognized["fields"]) == 4
    assert recognized["fields"][0]["control_kind"] == "dropdown"
    assert recognized["fields"][0]["options_status"] == "observed_subset"
    assert plan["suggestions"] == [] and plan["questions"] == []
    assert [item["code"] for item in plan["blocked"]] == [
        "observation_required", "entity_unresolved", "entity_unresolved", "manual_only"]
