"""Anonymous in-memory checkbox option identities and planning; no IO/API/model."""
from datetime import datetime, timezone
from unittest.mock import patch

from app import profile_service
from app.browser_models import BrowserSnapshot, FieldObservation, PageField
from app.field_semantics import (CHECKBOX_AMBIGUOUS_MEMORY_PREFIX, CHECKBOX_OPTION_SIGNATURE_PREFIX, enrich_fields,
                                 field_signature_for, normalize_text, option_fingerprint)
from app.form_agent import _local_safe_plan, _saved_answer_match, create_local_form_plan
from app.models import ApplicationAnswerMemory, CandidateProfile


URL = "https://anonymous-ats.example.test/application"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
OPTIONS = [f"匿名来源{index:02d}" for index in range(1, 15)]
FIELDS = enrich_fields([PageField(selector=f"#source-{index}", label=option, question_text="招聘信息来源",
    group_label="招聘信息来源", name="shared-sources", field_type="checkbox", required=True,
    option_label=option, option_value=f"source-{index}", control_group_key="anonymous-source-group",
    options=OPTIONS, multiple=False, current_value="true", label_source="container-owned", recognition_confidence=1,
    observation=FieldObservation(question_status="verified", options_status="group_complete"))
    for index, option in enumerate(OPTIONS)], URL)


def memory(field, value="是", **updates):
    data = dict(id=f"memory-{field.option_value}", question=field.question_text,
        normalized_question=normalize_text(field.question_text), semantic_key=field.semantic_key,
        entity_scope=field.entity_scope, field_signature=field.field_signature, field_type="checkbox",
        option_fingerprint=option_fingerprint(field.options), value=value, resume_id="anonymous-cv",
        company_scope="anonymous-ats.example.test", updated_at=NOW)
    return ApplicationAnswerMemory(**{**data, **updates})


def snapshot(fields=FIELDS):
    return BrowserSnapshot(session_id="anonymous-source-fixture", url=URL, title="匿名来源表单", fields=fields)


def plan(profile, fields=FIELDS):
    # Production knowledge/routing remains enabled, but any accidental DB or
    # network access fails rather than reading a real owner/profile.
    with patch("app.application_knowledge.retrieve_knowledge", return_value={}), \
            patch("app.storage._connection", side_effect=AssertionError("no personal database")):
        return create_local_form_plan(snapshot(fields), profile)


def run():
    assert len({field.field_signature for field in FIELDS}) == 14
    assert all(field.field_signature.startswith(CHECKBOX_OPTION_SIGNATURE_PREFIX) for field in FIELDS)
    assert all(field_signature_for(field, URL, 999) == field.field_signature for field in FIELDS)
    reordered = enrich_fields([field.model_copy(update={"selector": f"#new-{index}", "field_signature": ""})
                               for index, field in enumerate(reversed(FIELDS))], URL)
    assert {field.option_label: field.field_signature for field in reordered} == {
        field.option_label: field.field_signature for field in FIELDS}
    assert field_signature_for(FIELDS[0].model_copy(update={"option_value": "changed"}), URL) != FIELDS[0].field_signature
    assert field_signature_for(FIELDS[0].model_copy(update={"option_label": "另一个来源"}), URL) != FIELDS[0].field_signature

    # Each option has its own true/false decision. Matching one member must not
    # grant permission to clear a member for which no decision was confirmed.
    partial = CandidateProfile(application_answer_memory=[memory(FIELDS[0]), memory(FIELDS[1], "否")])
    before = partial.model_dump(mode="json")
    local = _local_safe_plan(snapshot(), partial)
    assert local.actions[0].action == "check" and local.actions[0].value is True
    assert local.actions[1].action == "check" and local.actions[1].value is False
    assert all(action.action == "ask_user" for action in local.actions[2:])
    routed = plan(partial)
    assert routed.actions[0].action == "check" and routed.actions[0].value is True
    assert routed.actions[1].action == "check" and routed.actions[1].value is False
    assert routed.actions[0].user_confirmed and routed.actions[1].user_confirmed
    assert all(action.action not in {"check", "fill", "select"} for action in routed.actions[2:])
    assert partial.model_dump(mode="json") == before

    full = CandidateProfile(application_answer_memory=[memory(field, "是" if index in {0, 2} else "否")
                                                       for index, field in enumerate(FIELDS)])
    actions = plan(full, reordered).actions
    checked = {field.option_label for field, action in zip(reordered, actions) if action.action == "check" and action.value is True}
    unchecked = [action for action in actions if action.action == "check" and action.value is False]
    assert checked == {OPTIONS[0], OPTIONS[2]} and len(unchecked) == 12

    # A previously unique learned question may become ambiguous on a later
    # page. Distinct DOM group/record markers cannot authorize cross-group
    # reuse, even when all labels, names, options and values happen to match.
    small = [field.model_copy(update={"options": OPTIONS[:2]}) for field in FIELDS[:2]]
    group_a = [field.model_copy(update={"selector": f"#group-a-{index}", "control_group_key": "group-a",
        "container_key": "transient-record-a", "section_path": ["临时栏目甲"]}) for index, field in enumerate(small)]
    group_b = [field.model_copy(update={"selector": f"#group-b-{index}", "control_group_key": "group-b",
        "container_key": "transient-record-b", "section_path": ["临时栏目乙"]}) for index, field in enumerate(small)]
    unique = enrich_fields(group_a, URL)
    learned_unique = CandidateProfile(application_answer_memory=[memory(unique[0], "否"), memory(unique[1], "是")])
    assert [action.value for action in plan(learned_unique, unique).actions] == [False, True]
    duplicate_groups = enrich_fields([*group_a, *group_b], URL)
    assert len(duplicate_groups) == 4
    assert all(not field.field_signature and field.knowledge_block_reason.startswith(CHECKBOX_AMBIGUOUS_MEMORY_PREFIX)
               for field in duplicate_groups)
    assert all(not _saved_answer_match(field, learned_unique).value for field in duplicate_groups)
    ambiguous_plan = plan(learned_unique, duplicate_groups)
    assert all(action.action not in {"check", "fill", "select"} for action in ambiguous_plan.actions)
    assert all(action.reason.startswith(CHECKBOX_AMBIGUOUS_MEMORY_PREFIX) for action in ambiguous_plan.actions)
    # Even a stale copied signature cannot bypass the explicit ambiguity mark.
    forged_old = duplicate_groups[0].model_copy(update={"field_signature": unique[0].field_signature})
    assert not _saved_answer_match(forged_old, learned_unique).value

    # A real semantic record scope distinguishes two questions. DOM identity
    # alone does not, but an explicit stable record scope remains reusable.
    scoped_a = [field.model_copy(update={"semantic_key": "project.custom", "entity_scope": "project:anonymous-a"}) for field in group_a]
    scoped_b = [field.model_copy(update={"semantic_key": "project.custom", "entity_scope": "project:anonymous-b"}) for field in group_b]
    scoped = enrich_fields([*scoped_a, *scoped_b], URL)
    assert len({field.field_signature for field in scoped}) == 4
    assert all(field.field_signature and not field.knowledge_block_reason for field in scoped)
    scoped_memory = CandidateProfile(application_answer_memory=[memory(scoped[0], "否"), memory(scoped[1], "是")])
    assert _saved_answer_match(scoped[0], scoped_memory).value == "否"
    assert _saved_answer_match(scoped[1], scoped_memory).value == "是"
    assert not _saved_answer_match(scoped[2], scoped_memory).value
    assert not _saved_answer_match(scoped[3], scoped_memory).value
    scoped_actions = plan(scoped_memory, scoped).actions
    assert scoped_actions[0].action == "check" and scoped_actions[0].value is False
    assert scoped_actions[1].action == "check" and scoped_actions[1].value is True
    assert all(action.action not in {"check", "fill", "select"} for action in scoped_actions[2:])

    # Legacy rank hashes, same-title semantic answers, and the unscoped legacy
    # answer dictionary never provide a multi-option checkbox's boolean state.
    legacy = CandidateProfile(application_answers={"招聘信息来源": "是"},
        application_answer_memory=[memory(FIELDS[0], field_signature="a" * 24),
                                   memory(FIELDS[0], field_signature="", value="否")])
    assert not _saved_answer_match(FIELDS[0], legacy).value
    stale = FIELDS[0].model_copy(update={"field_signature": "a" * 24})
    assert not _saved_answer_match(stale, legacy).value
    assert not any(action.action == "check" for action in plan(legacy).actions)
    for updates in ({"field_type": "text"}, {"semantic_key": "candidate.name"},
                    {"entity_scope": "third_party"}, {"normalized_question": "另一个问题"},
                    {"option_fingerprint": ""}, {"option_fingerprint": option_fingerprint(OPTIONS[:-1])},
                    {"value": OPTIONS[0]}, {"value": "unknown"}):
        assert not _saved_answer_match(FIELDS[0], CandidateProfile(application_answer_memory=[memory(FIELDS[0], **updates)])).value, updates
    changed = FIELDS[0].model_copy(update={"options": OPTIONS[:-1]})
    assert not _saved_answer_match(changed, full).value
    duplicate = FIELDS[0].model_copy(update={"options": [*OPTIONS, OPTIONS[0]]})
    assert not _saved_answer_match(duplicate, full).value

    # Standalone boolean signatures retain the existing rank algorithm and
    # same-question confirmed-boolean reuse; text/radio signatures are untouched.
    standalone = PageField(selector="#boolean", label="是否接收岗位通知", question_text="是否接收岗位通知",
        field_type="checkbox", options=["是", "否"], name="notify", label_source="label", recognition_confidence=1)
    assert not field_signature_for(standalone, URL).startswith(CHECKBOX_OPTION_SIGNATURE_PREFIX)
    assert field_signature_for(standalone, URL, 0) != field_signature_for(standalone, URL, 1)
    standalone = enrich_fields([standalone], URL)[0]
    standalone_profile = CandidateProfile(application_answer_memory=[memory(standalone, "否")])
    own = _local_safe_plan(snapshot([standalone]), standalone_profile).actions[0]
    assert own.action == "check" and own.value is False
    own_caption = standalone.model_copy(update={"control_group_key": "singleton-boolean", "option_label": standalone.question_text})
    assert not field_signature_for(own_caption, URL).startswith(CHECKBOX_OPTION_SIGNATURE_PREFIX)
    own_caption = enrich_fields([own_caption], URL)[0]
    own = _local_safe_plan(snapshot([own_caption]), CandidateProfile(application_answer_memory=[memory(own_caption, "是")])).actions[0]
    assert own.action == "check" and own.value is True
    for kind in ("text", "radio", "select-one"):
        ordinary = standalone.model_copy(update={"field_type": kind})
        assert not field_signature_for(ordinary, URL).startswith(CHECKBOX_OPTION_SIGNATURE_PREFIX)
        assert field_signature_for(ordinary, URL, 0) != field_signature_for(ordinary, URL, 1)

    # Exercise the existing persistence function using ONLY an in-memory store.
    # Existing upsert behavior can keep 14 separate stable option decisions;
    # correcting one option does not overwrite any other option.
    store = {"profile": CandidateProfile()}
    def saved(value):
        store["profile"] = CandidateProfile.model_validate(value.model_dump())
        return store["profile"]
    with patch.object(profile_service, "get_profile", lambda: store["profile"]), \
            patch.object(profile_service, "save_profile", saved), \
            patch("app.storage.get_resume", return_value=object()), \
            patch("app.storage._connection", side_effect=AssertionError("no personal database")):
        for index, field in enumerate(FIELDS):
            profile_service.save_application_answer(field.question_text, field.name, "是" if index == 0 else "否",
                semantic_key=field.semantic_key, entity_scope=field.entity_scope, field_signature=field.field_signature,
                field_type=field.field_type, options=field.options, source_url=URL, resume_id="anonymous-cv")
        assert len(store["profile"].application_answer_memory) == 14
        field = reordered[-1]
        profile_service.save_application_answer(field.question_text, field.name, "否", semantic_key=field.semantic_key,
            entity_scope=field.entity_scope, field_signature=field.field_signature, field_type=field.field_type,
            options=field.options, source_url=URL, resume_id="anonymous-cv")
        assert len(store["profile"].application_answer_memory) == 14
        assert _saved_answer_match(FIELDS[0], store["profile"]).value == "否"
        assert _saved_answer_match(FIELDS[1], store["profile"]).value == "否"
    print("checkbox_option_memory_test: OK (stable option identity, duplicate group rejection, scoped records, explicit true/false, in-memory persistence, unchanged standalone checkbox)")


if __name__ == "__main__":
    run()
