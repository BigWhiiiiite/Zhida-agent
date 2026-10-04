"""Synthetic, offline tests: education option memory is strict and nonmutating."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app.application_knowledge import KnowledgeMatch, KnowledgeRecord
from app.browser_models import BrowserSnapshot, PageField
from app.education_choice_memory import confirmed_education_choice
from app.field_semantics import normalize_text, option_fingerprint
from app.form_agent import create_local_form_plan
from app.models import ApplicationAnswerMemory, CandidateProfile, Education, ResumeProfile, ResumeRecord
from app.task_profile import compose_task_profile


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIELD = PageField(selector="#master-rank", label="专业排名", question_text="专业排名",
    field_type="combobox", options=["前20%", "前50%", "其他"],
    semantic_key="education.ranking", entity_scope="education:master", field_signature="current-signature")


def memory(**updates) -> ApplicationAnswerMemory:
    data = dict(id="test-choice", question="专业排名", normalized_question=normalize_text("专业排名"),
        semantic_key="education.ranking", entity_scope="education:master", field_signature="old-signature",
        field_type="combobox", option_fingerprint=option_fingerprint(FIELD.options), value="其他",
        resume_id="selected-cv", company_scope="example.test", confirmed_count=1, updated_at=NOW)
    return ApplicationAnswerMemory(**{**data, **updates})


def profile(*memories) -> CandidateProfile:
    return CandidateProfile(education=[Education(school="合成硕士院校", degree="硕士", ranking="专业前30%"),
        Education(school="合成本科院校", degree="本科", ranking="专业前20%")],
        application_answer_memory=list(memories) or [memory()])


current = profile()
before = current.model_dump(mode="json")
assert confirmed_education_choice(FIELD, current) == ("其他", "已确认答案记忆")
assert current.model_dump(mode="json") == before
assert current.education[0].ranking == "专业前30%"

# Same signature is not permission to bypass exact question, degree or options.
for update in ({"normalized_question": normalize_text("最高学历")}, {"entity_scope": "education:bachelor"},
               {"entity_scope": "education:unspecified"}, {"semantic_key": "education.degree"},
               {"option_fingerprint": ""}, {"option_fingerprint": option_fingerprint(["前20%", "其他"])},
               {"value": "前30%"}, {"field_type": "text"}):
    record = memory(field_signature=FIELD.field_signature, **update)
    assert confirmed_education_choice(FIELD, profile(record)) == ("", ""), update

for update in ({"options": []}, {"options": ["前20%", "其他"]}, {"field_type": "text"},
               {"multiple": True}, {"entity_scope": "education:unspecified"},
               {"entity_scope": "education:highest"}, {"entity_scope": "education:bachelor"},
               {"question_text": "我承诺专业排名真实并承担法律责任"},
               {"knowledge_block_reason": "尚未确定记录身份"}):
    assert confirmed_education_choice(FIELD.model_copy(update=update), current) == ("", ""), update

# A verified group scope may disambiguate an unspecified field, never override
# conflicting explicit page evidence; two masters also remain ambiguous.
unspecified = FIELD.model_copy(update={"entity_scope": "education:unspecified"})
assert confirmed_education_choice(unspecified, current, resolved_scope="education:master")[0] == "其他"
assert confirmed_education_choice(FIELD, current, resolved_scope="education:bachelor") == ("", "")
duplicate = current.model_copy(deep=True)
duplicate.education.append(Education(school="另一合成硕士院校", degree="硕士"))
assert confirmed_education_choice(FIELD, duplicate) == ("", "")

# Reordering options is safe; duplicate normalized labels are not uniquely selectable.
assert confirmed_education_choice(FIELD.model_copy(update={"options": list(reversed(FIELD.options))}), current)[0] == "其他"
assert confirmed_education_choice(FIELD.model_copy(update={"options": [*FIELD.options, "其 他"]}), current) == ("", "")

# The latest explicit correction wins regardless of input list order; a tie
# with conflicting values fails closed instead of taking whichever comes first.
newer = memory(id="new", value="前50%", updated_at=NOW + timedelta(days=1))
assert confirmed_education_choice(FIELD, profile(memory(), newer))[0] == "前50%"
assert confirmed_education_choice(FIELD, profile(memory(), memory(id="tie", value="前50%"))) == ("", "")

# Real task-context composition excludes another company's/CV's answer.
selected = ResumeRecord(id="selected-cv", label="合成简历", filename="fixture.pdf", file_size=1,
    status="completed", parser="fixture", profile=ResumeProfile(education=current.education),
    created_at=NOW, updated_at=NOW)
master = profile(memory(resume_id="another-cv"), memory(id="another-company", company_scope="another.test"))
filtered = compose_task_profile(master, selected, "example.test")
assert confirmed_education_choice(FIELD, filtered) == ("", "")
master.application_answer_memory.append(memory(id="matching"))
filtered = compose_task_profile(master, selected, "example.test")
assert confirmed_education_choice(FIELD, filtered)[0] == "其他"
assert confirmed_education_choice(FIELD, master) == ("", "")  # refuses mixed unfiltered tasks


def planned_action(task_profile: CandidateProfile, field: PageField = FIELD, *, knowledge: bool = False):
    """Exercise production knowledge enrichment, local mapping and safety routing.

    Only external knowledge retrieval is mocked; task composition and all field
    planning remain real. Any accidental database access fails the test.
    """
    evidence = {}
    if knowledge:
        record = KnowledgeRecord(id="confirmed-test-mapping", kind="mapping", question=field.question_text,
            source_url="https://jobs.example.test/apply", profile_path="education.ranking",
            entity_scope="education:master", field_type=field.field_type, confirmed=True,
            semantic_key="education.ranking", site_scope="example.test", created_at=NOW, updated_at=NOW)
        evidence[field.selector] = [KnowledgeMatch(record=record, score=1, exact=True, usable=True,
                                                  reason="合成的本人确认字段对应")]
    page = BrowserSnapshot(session_id="choice-memory-test", url="https://jobs.example.test/apply",
                           title="合成教育记录", fields=[field])
    with patch("app.application_knowledge.retrieve_knowledge", return_value=evidence), \
            patch("app.storage._connection", side_effect=AssertionError("must not access personal DB")):
        plan = create_local_form_plan(page, task_profile)
    return plan.actions[0]


for use_knowledge in (False, True):
    safe_task = compose_task_profile(master, selected, "example.test")
    original = safe_task.model_dump(mode="json")
    action = planned_action(safe_task, knowledge=use_knowledge)
    assert action.action == "select" and action.value == "其他" and not action.needs_model, action
    assert "已确认答案记忆" in action.value_source
    assert safe_task.model_dump(mode="json") == original
    assert selected.profile.education[0].ranking == "专业前30%"

    # A bachelor's answer must never serve a master's group, even if the DOM
    # signature happens to be reused by a template or trusted knowledge mapping.
    other_degree = compose_task_profile(profile(memory(entity_scope="education:bachelor",
        field_signature=FIELD.field_signature)), selected, "example.test")
    action = planned_action(other_degree, knowledge=use_knowledge)
    assert action.action == "ask_user" and action.value == "", action

    for change in ({"normalized_question": "其他教育问题", "field_signature": FIELD.field_signature},
                   {"option_fingerprint": ""}, {"option_fingerprint": option_fingerprint(["其他"])},
                   {"resume_id": "another-cv"}, {"company_scope": "another.test"}):
        mismatched = compose_task_profile(profile(memory(**change)), selected, "example.test")
        action = planned_action(mismatched, knowledge=use_knowledge)
        assert action.action == "ask_user" and action.value == "", (change, action)

    # Specific newest website confirmation wins over an otherwise exact generic
    # rank mapping; the source CV fact remains untouched and available elsewhere.
    exact_cv = selected.model_copy(deep=True)
    exact_cv.profile.education[0].ranking = "专业前20%"
    answers = profile(memory(value="前50%"), memory(id="newest", value="其他",
                                                    updated_at=NOW + timedelta(days=1)))
    exact_task = compose_task_profile(answers, exact_cv, "example.test")
    action = planned_action(exact_task, knowledge=use_knowledge)
    assert action.action == "select" and action.value == "其他", action
    assert exact_task.education[0].ranking == exact_cv.profile.education[0].ranking == "专业前20%"
    # Without same-task memory, normal exact factual mapping still works.
    exact_task.application_answer_memory = []
    action = planned_action(exact_task, knowledge=use_knowledge)
    assert action.action == "select" and action.value == "前20%", action

# Missing CV evidence cannot be sidestepped by an otherwise matching old answer.
missing_record = current.model_copy(update={"education": []})
assert planned_action(missing_record).action == "ask_user"
ambiguous_record = current.model_copy(deep=True)
ambiguous_record.education.append(Education(school="另一所硕士院校", degree="硕士", ranking="前20%"))
assert planned_action(ambiguous_record).action == "ask_user"
print("education_choice_memory_test: OK")
