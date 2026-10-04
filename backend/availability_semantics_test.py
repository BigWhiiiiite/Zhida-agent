"""Offline: future availability/native place never borrow employment/city facts."""
from app.browser_models import BrowserSnapshot, PageField
from app.field_semantics import entity_scope_for, semantic_key_for
from app.form_agent import _direct_profile_value, create_local_form_plan
from app.models import CandidateProfile, Experience


def run():
    availability = PageField(selector="#availability", label="最早可实习入职时间",
        question_text="最早可实习入职时间", section="个人信息", required=True,
        label_source="container-owned", field_type="date")
    past = PageField(selector="#past", label="入职时间", section="实习经历")
    native = PageField(selector="#native", label="籍贯", question_text="籍贯",
        section="个人信息", label_source="container-owned", required=True)
    assert semantic_key_for(availability) == "preference.available_date"
    assert entity_scope_for(availability) == "preference"
    assert semantic_key_for(past) == "experience.start_date"
    assert semantic_key_for(native) == "candidate.hometown"
    profile = CandidateProfile(location="北京", internships=[
        Experience(organization="匿名组织", start_date="2024-01")])
    assert _direct_profile_value(availability, profile) == ("", "")
    assert _direct_profile_value(native, profile) == ("", "")
    snapshot = BrowserSnapshot(session_id="fixture", url="https://fixture.example.test/form",
        title="匿名申请表", fields=[availability, native])
    plan = create_local_form_plan(snapshot, profile)
    assert all(a.action == "ask_user" and not a.needs_model for a in plan.actions)
    assert all(not a.value for a in plan.actions)
    confirmed = profile.model_copy(update={"available_date":"2026-12-01", "hometown":"河北省保定市"})
    assert _direct_profile_value(availability, confirmed) == ("2026-12-01", "主档案.available_date")
    assert _direct_profile_value(native, confirmed) == ("河北省保定市", "主档案.hometown")
    print("availability_semantics_test: OK (future vs past, native vs current city, no guessed facts)")


if __name__ == "__main__":
    run()
