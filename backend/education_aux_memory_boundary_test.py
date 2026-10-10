"""Pure in-memory education auxiliary-answer boundaries; no browser/model/DB.

An owned DOM education record is not a durable binding for its auxiliary
question. application.custom must not evade that boundary merely because it
is not an education.* attribute. Model confidence cannot create the binding.
"""
from datetime import datetime, timezone
from unittest.mock import patch

from app import form_agent
from app.browser_models import BrowserSnapshot, FillAction, FormPlan, PageField
from app.field_semantics import enrich_fields, normalize_text, option_fingerprint
from app.form_observation import annotate_fields
from app.models import ApplicationAnswerMemory, CandidateProfile, Education


URL = "https://education-aux-fixture.invalid/application"
QUESTION = "是否最高学历"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
AUTOMATIC = {"fill", "select", "check", "upload"}


def fixture(*, reverse=False):
    fields = []
    for owner, school, degree, major in (
            ("a", "匿名甲大学", "本科", "仅甲专业"),
            ("b", "匿名乙大学", "硕士", "仅乙专业")):
        provenance = dict(container_key=f"ant-resume-anonymous-{owner}",
            record_evidence="ant-resume-owned", section="教育经历", section_path=["教育经历"],
            entity_scope="education:unspecified", label_source="container-owned", recognition_confidence=1)
        for attribute, caption, value in (("school", "学校名称", school), ("degree", "学历", degree),
                                          ("major", "专业名称", major)):
            fields.append(PageField(selector=f"#{owner}-{attribute}", label=caption, question_text=caption,
                field_type="text", control_kind="text", semantic_key=f"education.{attribute}",
                current_value=value, **provenance))
        for index, option in enumerate(("是", "否")):
            fields.append(PageField(selector=f"#{owner}-highest-{index}", label=option, question_text=QUESTION,
                group_label=QUESTION, name="shared-highest-name", field_type="radio", control_kind="radio",
                semantic_key="application.custom", option_label=option, option_value=str(index), options=["是", "否"],
                options_capture="group_complete", control_group_key=f"anonymous-{owner}-highest",
                current_value="false", required=True, **provenance))
    if reverse:
        fields.reverse()
    fields = annotate_fields(enrich_fields(fields, URL))
    return BrowserSnapshot(session_id="anonymous-aux-memory", url=URL, title="匿名教育表单", fields=fields)


def memory(field, **updates):
    data = dict(id="anonymous-highest-memory", question=QUESTION,
        normalized_question=normalize_text(QUESTION), semantic_key=field.semantic_key,
        entity_scope=field.entity_scope, field_signature=field.field_signature,
        field_type=field.field_type, option_fingerprint=option_fingerprint(field.options), value="是",
        resume_id="anonymous-resume", company_scope="education-aux-fixture.invalid", updated_at=NOW)
    return ApplicationAnswerMemory(**{**data, **updates})


def profile_for(field, *, legacy=False, **memory_updates):
    return CandidateProfile(education=[
        Education(school="匿名甲大学", degree="本科", major="仅甲专业"),
        Education(school="匿名乙大学", degree="硕士", major="仅乙专业")],
        application_answer_memory=[] if legacy else [memory(field, **memory_updates)],
        application_answers={QUESTION: "是"} if legacy else {})


def auxiliaries(snapshot):
    return [field for field in snapshot.fields if field.question_text == QUESTION]


def assert_auxiliary_blocked(plan, snapshot, *, expected_count=4):
    selectors = {field.selector for field in auxiliaries(snapshot)}
    actions = [action for action in plan.actions if action.selector in selectors]
    assert len(actions) == expected_count
    assert all(action.action not in AUTOMATIC and action.value == "" for action in actions), [
        (action.selector, action.action, action.value) for action in actions]


def run():
    # Never open a user database, consult saved mappings or invoke a configured
    # model. Fail loudly if a tested path attempts any of those operations.
    with patch("app.storage._connection", side_effect=AssertionError("no personal database")), \
            patch.object(form_agent, "_knowledge_snapshot", side_effect=lambda snapshot: snapshot), \
            patch.object(form_agent, "configured_model", side_effect=AssertionError("no model configuration")), \
            patch.object(form_agent.Runner, "run", side_effect=AssertionError("no model request")):
        original = fixture()
        learned_field = next(field for field in auxiliaries(original) if field.selector == "#a-highest-0")
        assert len({field.container_key for field in auxiliaries(original)}) == 2
        assert all(field.observation.question_status == "verified" and
                   field.observation.options_status == "group_complete" for field in auxiliaries(original))
        for snapshot in (original, fixture(reverse=True)):
            for profile in (profile_for(learned_field), profile_for(learned_field, field_signature=""),
                            profile_for(learned_field, legacy=True)):
                before = profile.model_dump(mode="json")
                # Even an exact rank signature on the first record is not a
                # durable education identity; do not allow a same-title fallback
                # to grant a decision on the second record either.
                assert all(not form_agent._saved_answer_match(field, profile).value
                           for field in auxiliaries(snapshot))
                assert_auxiliary_blocked(form_agent._local_safe_plan(snapshot, profile), snapshot)
                routed = form_agent.create_local_form_plan(snapshot, profile)
                assert_auxiliary_blocked(routed, snapshot)
                assert all(not action.needs_model for action in routed.actions
                           if action.selector in {field.selector for field in auxiliaries(snapshot)})

                # Claimed confidence, user confirmation, profile pointers and
                # a model-supplied degree scope do not override actual DOM
                # record provenance on the target field.
                for claimed_scope in ("education:highest", "education:bachelor", "candidate"):
                    proposal = FormPlan(actions=[FillAction(selector=field.selector, label=QUESTION,
                        action="check", value=True, confidence=1, user_confirmed=True,
                        resolution_source="model", profile_path="education.degree",
                        entity_scope=claimed_scope, question_evidence=QUESTION,
                        value_source="model claims this is confirmed highest education")
                        for field in auxiliaries(snapshot)])
                    assert_auxiliary_blocked(form_agent._enforce_policy(proposal, snapshot, profile), snapshot)
                assert profile.model_dump(mode="json") == before

        # Ordinary candidate/application questions retain their prior confirmed
        # answer behavior; this is not a blanket ban on application.custom.
        ordinary = PageField(selector="#ordinary", label="是否接受异地工作", question_text="是否接受异地工作",
            label_source="explicit", field_type="checkbox", control_kind="checkbox",
            semantic_key="application.custom", entity_scope="application", options=["是", "否"])
        ordinary = annotate_fields(enrich_fields([ordinary], URL))[0]
        ordinary_memory = memory(ordinary, question=ordinary.question_text,
            normalized_question=normalize_text(ordinary.question_text))
        ordinary_profile = CandidateProfile(application_answer_memory=[ordinary_memory])
        assert form_agent._saved_answer_match(ordinary, ordinary_profile).value == "是"
        ordinary_snapshot = original.model_copy(update={"fields": [ordinary]})
        action = form_agent._local_safe_plan(ordinary_snapshot, ordinary_profile).actions[0]
        assert action.action == "check" and action.value is True
    print("education_aux_memory_boundary_test: OK (two complete records, exact/semantic/legacy memory rejection, reorder, model-policy boundary, ordinary-question reuse; in-memory only)")


if __name__ == "__main__":
    run()
