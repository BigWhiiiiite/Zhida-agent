"""Synthetic D2 matching tests: no browser, network, model or database required."""
from copy import deepcopy

import pytest

from app.backend_v2.answer_planning import build_answer_plan, validate_confirmation_answer
from app.backend_v2.form_recognition import MAX_FIELDS


def context(**overrides):
    return {"user_id": "user-a", "resume_revision_id": "resume-agent-v2", "site_scope": "jobs.example.test",
            "now": "2026-10-12T00:00:00+00:00", "entities": [], **overrides}


def field(label="姓名", **overrides):
    return {"id": "field-1", "question": label, "question_status": "verified", "matchable": True,
            "control_kind": "text", "options_status": "not_applicable", "options": [], "required": True,
            "section_path": ["个人基本信息"], "entity_binding": {"status": "not_applicable"},
            "evidence_refs": ["observation:1:label:1"], **overrides}


def recognition(*fields, **overrides):
    return {"schema_version": "form-recognition-v1", "fields": list(fields), "needs_observation": False,
            "coverage": {"application_complete": False}, **overrides}


def fact(key="name", value="测试同学", **overrides):
    return {"id": "fact-1", "user_id": "user-a", "revision_id": "fact-rev-1", "revision_no": 1,
            "subject_id": "person", "semantic_key": key, "value": value, "scope_key": "global",
            "confirmation": "confirmed", "valid_until": None, **overrides}


def option(label, **overrides):
    return {"label": label, "value": "dom-" + label, "disabled": False,
            "evidence_refs": ["observation:1:option:" + label], **overrides}


def education_field(label="学院", **overrides):
    binding = {"kind": "education", "record_key": "dom-record-1", "status": "record_observed",
               "anchors": [{"semantic_key": "school", "value": "甲大学", "evidence_refs": ["school-label"]},
                           {"semantic_key": "degree", "value": "硕士", "evidence_refs": ["degree-label"]}]}
    return field(label, section_path=["教育经历"], entity_binding=binding, **overrides)


def education_context(**overrides):
    return context(entities=[
        {"entity_id": "bachelor-id", "entity_type": "education", "education_level": "本科",
         "content": {"school": "甲大学", "college": "本科学院"}},
        {"entity_id": "master-id", "entity_type": "education", "education_level": "硕士",
         "content": {"school": "甲大学", "college": "研究生学院"}},
    ], **overrides)


def test_exact_confirmed_name_produces_only_read_only_suggestion():
    plan = build_answer_plan(recognition(field()), [fact()], context())
    assert plan["suggestions"][0]["value"] == "测试同学"
    assert plan["suggestions"][0]["fact_revision_refs"] == ["fact-rev-1"]
    assert plan["suggestions"][0]["evidence_refs"] == ["observation:1:label:1"]
    assert plan["suggestions"][0]["auto_fill_allowed"] is False
    assert plan["read_only"] is True and plan["ready_to_execute"] is False
    assert plan["executable_actions"] == [] and plan["coverage"]["application_complete"] is False


def test_planner_shares_the_recognition_field_limit_without_truncation():
    with pytest.raises(ValueError, match="上限"):
        build_answer_plan(recognition(*[field(id=str(i)) for i in range(MAX_FIELDS + 1)]), [], context())


@pytest.mark.parametrize("override", [
    {"user_id": "other-user"}, {"user_id": None}, {"confirmation": "unconfirmed"},
    {"confirmation": "revoked"}, {"valid_until": "2026-10-11T23:59:59+00:00"},
    {"valid_until": "2027-01-01"}, {"valid_until": "bad timestamp"}, {"revision_id": None},
    {"scope_key": "resume:other-version"}, {"scope_key": "site:other.example.test"},
    {"scope_key": "entity:other-person"}, {"subject_id": "emergency-contact"},
])
def test_foreign_unconfirmed_expired_and_out_of_scope_facts_never_match(override):
    plan = build_answer_plan(recognition(field()), [fact(**override)], context())
    assert not plan["suggestions"]
    assert plan["questions"][0]["label"] == "姓名"


def test_unverified_title_or_semantic_hint_cannot_become_a_question():
    plan = build_answer_plan(recognition(field("是", semantic_key="early_internship")), [], context())
    assert plan["blocked"][0]["code"] == "semantic_unresolved"
    assert plan["questions"] == []
    plan = build_answer_plan(recognition(field(question_status="unresolved")), [fact()], context())
    assert plan["blocked"][0]["code"] == "observation_required"


def test_missing_options_is_system_observation_problem_not_a_blind_user_question():
    f = field("意向工作城市", control_kind="dropdown", options_status="unavailable")
    plan = build_answer_plan(recognition(f), [fact("desired_work_city", "北京")], context())
    assert not plan["questions"] and not plan["suggestions"]
    assert plan["blocked"][0]["code"] == "options_incomplete"


def test_exact_dropdown_choice_is_grounded_in_enabled_observed_option():
    f = field("意向工作城市", control_kind="dropdown", options_status="native_complete",
              options=[option("北京"), option("上海")])
    plan = build_answer_plan(recognition(f), [fact("desired_work_city", "北京")], context())
    suggestion = plan["suggestions"][0]
    assert suggestion["value"] == "北京"
    assert suggestion["option_refs"][0]["value"] == "dom-北京"
    assert suggestion["scope_key"] == "site:jobs.example.test"


def test_similar_option_requires_confirmation_not_fuzzy_match():
    options = [option("北京分行")]
    f = field("意向工作城市", control_kind="dropdown", options_status="native_complete", options=options)
    plan = build_answer_plan(recognition(f), [fact("desired_work_city", "北京")], context())
    assert not plan["suggestions"]
    assert plan["questions"][0]["label"] == "意向工作城市"


@pytest.mark.parametrize("options", [[], [option("北京", disabled=True)],
    [option("北京", evidence_refs=[])], [option("北京"), option("北京", value="different-value")]])
def test_disabled_duplicate_empty_or_ungrounded_options_are_system_problem(options):
    f = field("意向工作城市", control_kind="dropdown", options_status="native_complete", options=options)
    plan = build_answer_plan(recognition(f), [fact("desired_work_city", "北京")], context())
    assert not plan["suggestions"] and not plan["questions"]
    assert plan["blocked"][0]["code"] == "options_ambiguous"


def test_complete_question_male_female_group_is_not_two_yes_no_questions():
    f = field("性别", control_kind="radio", options_status="group_complete", options=[option("男"), option("女")])
    plan = build_answer_plan(recognition(f), [fact("gender", "男性")], context())
    assert plan["suggestions"][0]["value"] == "男" and not plan["questions"]


def test_real_interview_option_is_not_overridden_by_residential_city():
    f = field("参加面试城市", control_kind="dropdown", options_status="native_complete", options=[option("远程面试")])
    plan = build_answer_plan(recognition(f), [fact("current_location", "北京")], context())
    assert not plan["suggestions"]
    assert plan["questions"][0]["options"] == ["远程面试"]
    assert plan["questions"][0]["semantic_key"] == "interview_city"


def test_emergency_contact_name_and_phone_never_reuse_applicant_identity():
    fields = [field("姓名", section_path=["紧急联系人"], id="name"),
              field("联系电话", section_path=["紧急联系人"], id="phone")]
    plan = build_answer_plan(recognition(*fields), [fact(), fact("phone", "123")], context())
    assert not plan["suggestions"]
    assert [q["semantic_key"] for q in plan["questions"]] == ["emergency_contact_name", "emergency_contact_phone"]


def test_family_name_is_not_guessed_as_self_name():
    plan = build_answer_plan(recognition(field(section_path=["家庭成员"])), [fact()], context())
    assert not plan["suggestions"] and not plan["questions"]
    assert plan["blocked"][0]["code"] == "semantic_unresolved"


def test_undergraduate_and_master_colleges_are_bound_by_anchors_not_position():
    facts = [fact("college", "本科学院", subject_id="bachelor-id"),
             fact("college", "研究生学院", subject_id="master-id", revision_id="master-fact")]
    plan = build_answer_plan(recognition(education_field()), facts, education_context())
    assert plan["suggestions"][0]["value"] == "研究生学院"
    assert plan["suggestions"][0]["subject_id"] == "master-id"
    assert plan["suggestions"][0]["scope_key"] == "resume:resume-agent-v2"


def test_ambiguous_school_or_claimed_profile_id_cannot_identify_education():
    f = education_field()
    f["entity_binding"]["anchors"] = f["entity_binding"]["anchors"][:1]
    f["entity_binding"]["entity_id"] = "master-id"
    plan = build_answer_plan(recognition(f), [fact("college", "研究生学院", subject_id="master-id")], education_context())
    assert plan["blocked"][0]["code"] == "entity_unresolved" and not plan["suggestions"]


def test_unproven_education_anchor_and_unknown_record_boundary_block_matching():
    f = education_field()
    for anchor in f["entity_binding"]["anchors"]:
        anchor["evidence_refs"] = []
    plan = build_answer_plan(recognition(f), [], education_context())
    assert plan["blocked"][0]["code"] == "entity_unresolved"
    f["entity_binding"]["status"] = "unresolved"
    assert build_answer_plan(recognition(f), [], education_context())["blocked"]


def test_resume_entity_content_is_evidence_not_confirmed_fact():
    plan = build_answer_plan(recognition(education_field()), [], education_context())
    assert not plan["suggestions"]
    assert plan["questions"][0]["subject_id"] == "master-id"


def test_foreign_or_old_version_entity_cannot_match():
    ctx = education_context()
    ctx["entities"][1]["resume_revision_id"] = "old-version"
    assert build_answer_plan(recognition(education_field()), [], ctx)["blocked"]
    ctx = education_context()
    ctx["entities"][1]["user_id"] = "other-user"
    assert build_answer_plan(recognition(education_field()), [], ctx)["blocked"]


def test_degree_and_academic_degree_are_not_the_same_fact():
    plan = build_answer_plan(recognition(education_field("学位")),
                             [fact("degree", "硕士", subject_id="master-id")], education_context())
    assert not plan["suggestions"]
    assert plan["questions"][0]["semantic_key"] == "academic_degree"


def test_exact_target_scope_answer_overrides_global_default_without_reasking():
    f = field("意向工作城市")
    facts = [fact("desired_work_city", "北京"), fact("desired_work_city", "上海", scope_key="site:jobs.example.test",
                                                     revision_id="rev-site", revision_no=3)]
    plan = build_answer_plan(recognition(f), facts, context())
    assert plan["suggestions"][0]["value"] == "上海"
    assert plan["suggestions"][0]["fact_revision_refs"] == ["rev-site"]
    assert not plan["questions"]


def test_conflicting_nontarget_scopes_require_confirmation():
    facts = [fact("desired_work_city", "北京"), fact("desired_work_city", "上海", scope_key="resume:resume-agent-v2")]
    plan = build_answer_plan(recognition(field("意向工作城市")), facts, context())
    assert not plan["suggestions"] and plan["questions"]
    assert plan["questions"][0]["expected_fact_revision"] == 0


@pytest.mark.parametrize("state", ["revoked", "unconfirmed"])
def test_revoked_or_unconfirmed_exact_scope_does_not_revive_global_answer(state):
    facts = [fact("desired_work_city", "北京"), fact("desired_work_city", "上海", scope_key="site:jobs.example.test",
                                                     revision_no=3, confirmation=state)]
    plan = build_answer_plan(recognition(field("意向工作城市")), facts, context())
    assert not plan["suggestions"]
    assert plan["questions"][0]["expected_fact_revision"] == 3


def test_other_scope_revision_does_not_become_target_update_version():
    f = field("意向工作城市", control_kind="dropdown", options_status="native_complete", options=[option("上海")])
    plan = build_answer_plan(recognition(f), [fact("desired_work_city", "北京", revision_no=5)], context())
    assert plan["questions"][0]["expected_fact_revision"] == 0


def test_rank_is_not_rounded_to_a_more_impressive_option():
    f = education_field("专业排名", control_kind="dropdown", options_status="native_complete",
                        options=[option("前20%"), option("前50%"), option("其他")])
    plan = build_answer_plan(recognition(f), [fact("education_rank", "前30%", subject_id="master-id")], education_context())
    assert not plan["suggestions"]
    assert plan["questions"][0]["options"] == ["前20%", "前50%", "其他"]


@pytest.mark.parametrize("value", ["2003", "2003-01", "2003-02-30", "至今"])
def test_day_date_does_not_invent_missing_precision_or_accept_invalid_date(value):
    f = field("出生日期", control_kind="date", date_precision="day")
    plan = build_answer_plan(recognition(f), [fact("birth_date", value)], context())
    assert not plan["suggestions"] and plan["questions"]


def test_valid_exact_date_and_month_date_keep_the_requested_precision():
    f = field("出生日期", control_kind="date", date_precision="day")
    assert build_answer_plan(recognition(f), [fact("birth_date", "2003.1.19")], context())["suggestions"][0]["value"] == "2003-01-19"
    f["date_precision"] = "month"
    assert build_answer_plan(recognition(f), [fact("birth_date", "2003-01")], context())["suggestions"][0]["value"] == "2003-01"


def test_month_first_requires_explicit_scoped_confirmed_policy_and_evidence():
    f = education_field("入学时间", control_kind="date", date_precision="day")
    facts = [fact("start_date", "2025-09", subject_id="master-id")]
    policy = {"month_date_placeholder": {"confirmed": True, "day": 1, "scope_key": "resume:resume-agent-v2",
                                         "semantic_keys": ["start_date", "end_date"],
                                         "evidence_ref": "user-confirmation:month-first"}}
    plan = build_answer_plan(recognition(f), facts, education_context(policies=policy))
    assert plan["suggestions"][0]["value"] == "2025-09-01"
    assert plan["suggestions"][0]["policy_refs"] == ["user-confirmation:month-first"]
    policy["month_date_placeholder"]["scope_key"] = "resume:other"
    assert not build_answer_plan(recognition(f), facts, education_context(policies=policy))["suggestions"]


def test_experience_month_placeholder_policy_does_not_invent_a_birthday():
    policy = {"month_date_placeholder": {"confirmed": True, "day": 1, "scope_key": "global",
                                         "semantic_keys": ["start_date", "end_date"], "evidence_ref": "confirmed-policy"}}
    f = field("出生日期", control_kind="date", date_precision="day")
    plan = build_answer_plan(recognition(f), [fact("birth_date", "2003-01")], context(policies=policy))
    assert not plan["suggestions"] and plan["questions"]


def test_unknown_date_precision_is_recognition_problem_even_when_fact_known():
    plan = build_answer_plan(recognition(field("出生日期", control_kind="date")), [fact("birth_date", "2003-01-19")], context())
    assert plan["blocked"][0]["code"] == "date_precision_unknown" and not plan["questions"]


@pytest.mark.parametrize("f", [field("上传简历", control_kind="file"), field("我同意隐私协议", control_kind="checkbox"),
                               field("本人承诺资料真实"), field("现居住地", control_kind="cascader")])
def test_upload_declaration_and_unimplemented_cascader_never_become_actions(f):
    plan = build_answer_plan(recognition(f), [fact()], context())
    assert plan["blocked"] and not plan["suggestions"] and plan["executable_actions"] == []


def test_rag_candidate_never_becomes_confirmed_answer_even_when_claiming_authority():
    item = {"user_id": "user-a", "entity_id": "person", "field_id": "field-1", "chunk_id": "chunk-1",
            "source_revision_id": "source-rev", "value": "猜测姓名", "auto_fill_allowed": True,
            "trust": "confirmed_fact", "resume_revision_id": "resume-agent-v2"}
    plan = build_answer_plan(recognition(field()), [], context(), [item])
    assert not plan["suggestions"]
    refs = plan["questions"][0]["retrieval_refs"]
    assert refs == [{"chunk_id": "chunk-1", "source_revision_id": "source-rev", "auto_fill_allowed": False}]
    assert "猜测姓名" not in str(plan)


def test_cross_tenant_resume_entity_and_site_retrieval_candidates_are_not_exposed():
    base = {"user_id": "user-a", "entity_id": "person", "field_id": "field-1", "chunk_id": "secret-chunk",
            "source_revision_id": "source-rev"}
    candidates = [{**base, **override} for override in [{"user_id": "other"}, {"entity_id": "other"},
                  {"resume_revision_id": "other"}, {"site_scope": "other.test"}, {"field_id": "other"}]]
    plan = build_answer_plan(recognition(field()), [], context(), candidates)
    assert plan["questions"][0]["retrieval_refs"] == []
    assert "secret-chunk" not in str(plan)


def test_input_is_not_mutated_and_frontend_current_value_is_not_authority():
    observed = recognition(field(current_value="误填姓名"))
    original = deepcopy(observed)
    plan = build_answer_plan(observed, [], context())
    assert not plan["suggestions"] and plan["questions"]
    assert observed == original


def test_yes_no_boolean_fact_uses_question_semantics_and_observed_options():
    f = field("是否可提前实习", control_kind="radio", options_status="group_complete", options=[option("是"), option("否")])
    plan = build_answer_plan(recognition(f), [fact("early_internship", False)], context())
    assert plan["suggestions"][0]["value"] == "否"


def test_checkbox_preserves_multi_value_and_single_choice_rejects_multiple():
    f = field("意向工作城市", control_kind="checkbox", options_status="group_complete", options=[option("北京"), option("上海")])
    facts = [fact("desired_work_city", ["北京", "上海"])]
    assert build_answer_plan(recognition(f), facts, context())["suggestions"][0]["value"] == ["北京", "上海"]
    f["control_kind"] = "dropdown"
    assert build_answer_plan(recognition(f), facts, context())["questions"]


def test_binding_and_normalization_contract_are_required():
    with pytest.raises(ValueError, match="绑定"):
        build_answer_plan(recognition(field()), [], context(user_id=None))
    with pytest.raises(ValueError, match="规范化"):
        build_answer_plan({"fields": [field()]}, [], context())
    with pytest.raises(ValueError, match="时区"):
        build_answer_plan(recognition(field()), [], context(now="2026-10-12"))


def test_normalizer_prefixed_anchor_keys_are_supported():
    f = education_field()
    for anchor in f["entity_binding"]["anchors"]:
        anchor["semantic_key"] = "education." + anchor["semantic_key"]
    facts = [fact("college", "研究生学院", subject_id="master-id")]
    assert build_answer_plan(recognition(f), facts, education_context())["suggestions"][0]["value"] == "研究生学院"


def test_formal_work_section_cannot_consume_internship_experience():
    f = field("公司名称", section_path=["正式工作经历"], entity_binding={"kind": "experience", "status": "record_observed",
              "anchors": [{"semantic_key": "experience.organization", "value": "甲公司", "evidence_refs": ["anchor-1"]}]})
    ctx = context(entities=[{"entity_id": "intern-1", "entity_type": "internship", "content": {"organization": "甲公司"}}])
    facts = [fact("organization", "甲公司", subject_id="intern-1")]
    assert build_answer_plan(recognition(f), facts, ctx)["blocked"][0]["code"] == "entity_unresolved"
    f["section_path"] = ["实习经历"]
    assert build_answer_plan(recognition(f), facts, ctx)["suggestions"][0]["value"] == "甲公司"


def test_duplicate_field_id_blocks_both_entries_instead_of_retaining_first():
    plan = build_answer_plan(recognition(field(), field()), [fact()], context())
    assert not plan["suggestions"] and len(plan["blocked"]) == 2


def test_real_normalizer_contract_for_text_and_label_only_select():
    from app.backend_v2.form_recognition import normalize_observation

    def raw(question, selector, **extra):
        return {"selector": selector, "label": question, "label_source": "explicit", "field_type": "text",
                "control_kind": "text", "control_evidence": "HTML input", **extra}

    observed = normalize_observation({"fields": [raw("姓名", "#name"),
        raw("期望工作城市", "#city", field_type="select-one", control_kind="native_select",
            options=["请选择", "北京", "上海"], options_capture="native_complete")],
        "extraction_report": {"scope": "current_visible_document", "capture_status": "observed",
            "observed_controls": 2, "captured_controls": 2, "unmapped_controls": 0,
            "pending_sections": [], "embedded_regions": 0, "unread_shadow_regions": 0}})
    plan = build_answer_plan(observed, [fact(), fact("desired_work_city", "北京")], context())
    assert [item["value"] for item in plan["suggestions"]] == ["测试同学", "北京"]
    assert plan["suggestions"][1]["option_refs"][0]["value"] is None
    assert plan["executable_actions"] == []


def test_real_normalizer_education_anchors_bind_only_the_master_college():
    from app.backend_v2.form_recognition import normalize_observation

    fields = []
    for index, (label, value) in enumerate([("学校名称", "甲大学"), ("学历", "硕士"), ("学院名称", "")]):
        fields.append({"selector": f"#edu-{index}", "label": label, "label_source": "explicit",
                       "field_type": "text", "control_kind": "text", "control_evidence": "HTML input",
                       "section_path": ["教育经历"], "container_key": "verified-master-record",
                       "record_evidence": "ant-resume-owned", "current_value": value})
    observed = normalize_observation({"fields": fields})
    facts = [fact("college", "研究生学院", subject_id="master-id"), fact("college", "本科学院", subject_id="bachelor-id")]
    plan = build_answer_plan(observed, facts, education_context())
    assert len(plan["suggestions"]) == 1
    assert plan["suggestions"][0]["value"] == "研究生学院"
    assert plan["suggestions"][0]["subject_id"] == "master-id"


def test_html_date_precision_alias_is_supported_by_planner_and_validator():
    f = field("出生日期", control_kind="date", date_precision="date")
    plan = build_answer_plan(recognition(f), [fact("birth_date", "2003-01-19")], context())
    assert plan["suggestions"][0]["value"] == "2003-01-19"
    assert validate_confirmation_answer("date", [], "2003-01-19", "date") == "2003-01-19"


@pytest.mark.parametrize("kind,options,value,precision", [
    ("checkbox", ["北京", "上海"], ["北京", "上海"], None),
    ("dropdown", ["北京", "上海"], "北京", None),
    ("radio", ["是", "否"], "否", None),
    ("text", [], "测试回答", None), ("text", [], 190, None), ("text", [], 0, None),
    ("date", [], "2024-02-29", "day"), ("date", [], "2024-02", "month"),
    ("date", [], "2024", "year"),
])
def test_typed_confirmation_accepts_only_valid_values_without_coercion(kind, options, value, precision):
    assert validate_confirmation_answer(kind, options, value, precision) == value


@pytest.mark.parametrize("kind,options,value,precision", [
    ("checkbox", ["北京", "上海"], "北京", None),
    ("checkbox", ["北京", "上海"], [], None),
    ("checkbox", ["北京", "上海"], ["北京", "北京"], None),
    ("checkbox", ["北京", "上海"], ["深圳"], None),
    ("checkbox", ["北京", "上海"], [True], None),
    ("dropdown", ["北京", "上海"], ["北京"], None),
    ("radio", ["是", "否"], True, None),
    ("dropdown", ["北京", "上海"], "北京市", None),
    ("radio", ["是", "是"], "是", None),
    ("dropdown", [], "北京", None),
    ("text", [], "   ", None), ("text", [], True, None), ("text", [], [], None),
    ("text", [], {"value": "北京"}, None), ("text", [], float("nan"), None),
    ("text", [], float("inf"), None),
    ("date", [], "2023-02-29", "day"), ("date", [], "2024-02", "day"),
    ("date", [], "2024-02-29", "month"), ("date", [], "2024-13", "month"),
    ("date", [], "2024-02-29", None), ("date", [], "0000", "year"),
    ("file", [], "简历.pdf", None), ("cascader", ["北京"], "北京", None),
])
def test_typed_confirmation_rejects_invalid_or_unobserved_values(kind, options, value, precision):
    with pytest.raises(ValueError):
        validate_confirmation_answer(kind, options, value, precision)


def test_repeated_same_fact_question_is_asked_once_with_all_field_references():
    plan = build_answer_plan(recognition(field("姓名", id="f1"), field("真实姓名", id="f2")), [], context())
    assert len(plan["questions"]) == 1
    assert plan["questions"][0]["field_ids"] == ["f1", "f2"]
    assert plan["questions"][0]["source_questions"] == ["姓名", "真实姓名"]


def test_conflicting_controls_for_same_fact_are_system_problem_not_duplicate_questions():
    f1 = field("意向城市", id="f1", control_kind="radio", options_status="group_complete", options=[option("北京"), option("上海")])
    f2 = field("期望工作城市", id="f2", control_kind="radio", options_status="group_complete", options=[option("深圳"), option("北京")])
    plan = build_answer_plan(recognition(f1, f2), [], context())
    assert plan["questions"] == []
    assert len(plan["blocked"]) == 2
    assert all(item["code"] == "confirmation_scope_conflict" for item in plan["blocked"])


def test_same_option_set_with_different_order_is_one_confirmation():
    f1 = field("意向城市", id="f1", control_kind="radio", options_status="group_complete", options=[option("北京"), option("上海")])
    f2 = field("期望工作城市", id="f2", control_kind="radio", options_status="group_complete", options=[option("上海"), option("北京")])
    plan = build_answer_plan(recognition(f1, f2), [], context())
    assert len(plan["questions"]) == 1 and not plan["blocked"]


def test_nonfinite_fact_and_repeated_checkbox_values_are_not_suggested():
    assert not build_answer_plan(recognition(field("身高")), [fact("height_cm", float("nan"))], context())["suggestions"]
    f = field("意向城市", control_kind="checkbox", options_status="group_complete", options=[option("北京")])
    assert not build_answer_plan(recognition(f), [fact("desired_work_city", ["北京", "北京"])], context())["suggestions"]
