"""Isolated source binding: names/URLs, no cross-record/date/order guessing."""
from app.browser_models import BrowserSnapshot, PageField
from datetime import datetime, timezone
from app.models import CandidateProfile, Education, Experience, Project, ApplicationAnswerMemory
from app.repeated_records import resolve_repeated_records, repeated_value
from app.form_agent import _local_safe_plan, _education_resolutions, build_form_review


def field(group, key, value=""):
    return PageField(selector=group+key, container_key=group, label=key,
                     semantic_key=key, current_value=value, label_source="explicit")


def snap(fields):
    return BrowserSnapshot(session_id="fixture", url="https://example.test/form", title="form", fields=fields)


def run():
    profile = CandidateProfile(internships=[Experience(organization="甲公司", role="工程师"),
        Experience(organization="乙公司", role="开发实习生")], projects=[
        Project(name="智能求职工具调用项目", project_url="https://example.test/project-a", description="开发。", achievements=["验证。"]),
        Project(name="多智能体协作分析项目", role="核心开发")])
    fields = [field("w1", "experience.organization", "乙公司"), field("w1", "experience.role"),
              field("w2", "experience.organization"), field("w2", "experience.role"),
              field("p1", "project.name", "项目链接:https://example.test/project-a"), field("p1", "project.description"),
              field("p2", "project.name"), field("p2", "project.description", "多智能体协作分析项目：验证分析流程")]
    bindings = resolve_repeated_records(snap(fields), profile)
    assert len(bindings) == 8
    assert bindings["w1experience.role"][1].organization == "乙公司"
    assert bindings["w2experience.role"][1].organization == "甲公司"
    assert repeated_value(bindings["p1project.description"], "project.description") == "开发。\n验证。"
    plan = _local_safe_plan(snap(fields), profile)
    assert next(a for a in plan.actions if a.selector == "w1experience.role").value == "开发实习生"
    # Name+URL disagreement blocks the entire record; no nearest-name guess.
    bad = [field("a", "project.name", "多智能体协作分析项目"),
           field("a", "project.description", "https://example.test/project-a"), field("b", "project.name")]
    assert not resolve_repeated_records(snap(bad), profile)
    duplicates = [field("a", "experience.organization", "甲公司"), field("b", "experience.organization", "甲公司")]
    assert not resolve_repeated_records(snap(duplicates), profile)
    assert not resolve_repeated_records(snap([field("a", "experience.organization"), field("b", "experience.organization")]), profile)
    assert not resolve_repeated_records(snap([field("a", "project.name", "https://example.test/project-a-evil")]), profile)
    # A remaining blank education group is not selected by source list order.
    profile.education = [Education(school="硕士校", degree="硕士"), Education(school="本科校", degree="本科")]
    a, b = field("e1", "education.school"), field("e2", "education.school")
    a.entity_scope = "education:bachelor"
    b.entity_scope = "education:unspecified"
    result = _education_resolutions(snap([a,b]), profile)
    assert result[b.selector].record.school == "硕士校"
    b.current_value = "未确认大学"
    assert _education_resolutions(snap([a,b]), profile)[b.selector].record is None
    dated = snap([field("w", "experience.organization", "乙公司"), field("w", "experience.start_date")])
    dated.url = "https://talent.autohome.com.cn/recruit-delivery.html?pid=fixture"
    profile.internships[1].start_date = "2024.07"
    action = next(a for a in _local_safe_plan(dated, profile).actions if a.label == "experience.start_date")
    assert action.action == "ask_user" and action.resolution_source == "user"
    profile.application_answer_memory = [ApplicationAnswerMemory(id="date-policy", semantic_key="application.date_precision",
        question="年月精度日期的网申填写约定", normalized_question="年月精度日期的网申填写约定",
        source_host="talent.autohome.com.cn", value="每月1日作为月份占位，不代表真实精确日期；至今保留",
        updated_at=datetime.now(timezone.utc))]
    action = next(a for a in _local_safe_plan(dated, profile).actions if a.label == "experience.start_date")
    assert action.action == "fill" and action.value == "2024-07-01" and action.user_confirmed
    profile.application_answer_memory[0].source_host = "other.test"
    assert next(a for a in _local_safe_plan(dated, profile).actions if a.label == "experience.start_date").action == "ask_user"
    # Bound Phoenix calendar and ongoing controls must use the same source.
    profile.internships[1].end_date = '至今'
    month = field('w', 'experience.start_date'); month.field_type='combobox'; month.date_precision='month'
    end = field('w', 'experience.end_date'); end.field_type='combobox'; end.date_precision='month'
    ongoing = field('w', 'experience.current', 'false'); ongoing.field_type='checkbox'
    phoenix = snap([field('w', 'experience.organization', '乙公司'), month, end, ongoing])
    planned = {a.selector:a for a in _local_safe_plan(phoenix, profile).actions}
    assert planned[month.selector].action == 'select' and planned[month.selector].value == '2024-07'
    assert planned[end.selector].action == 'skip'
    assert planned[ongoing.selector].action == 'check' and planned[ongoing.selector].value is True
    # False is a legitimate, source-backed choice for a past record.
    profile.internships[1].end_date = '2024-10'
    planned = {a.selector:a for a in _local_safe_plan(phoenix, profile).actions}
    assert planned[ongoing.selector].action == 'check' and planned[ongoing.selector].value is False
    checked_review = build_form_review(phoenix, _local_safe_plan(phoenix, profile))
    assert next(a for a in checked_review.plan.actions if a.selector == ongoing.selector).action == 'skip'
    # Duties are a separate record fact, not the role name or a neighboring
    # project's description; confirmed duties can be reused across companies.
    profile.projects[0].responsibilities = '负责工具调用编排与回读验证'
    duties = snap([field('p', 'project.name', profile.projects[0].name), field('p', 'project.responsibilities')])
    for host in ('company-a.test', 'company-b.test'):
        duties.url = 'https://' + host + '/form'
        action = next(a for a in _local_safe_plan(duties, profile).actions if a.selector == 'pproject.responsibilities')
        assert action.action == 'fill' and action.value == profile.projects[0].responsibilities
    duties.fields[0].current_value = profile.projects[1].name
    assert next(a for a in _local_safe_plan(duties, profile).actions if a.selector == 'pproject.responsibilities').action == 'ask_user'
    # A fallback is verbatim and limited to the same source record. Generic
    # team outcomes, roles and metrics are not personal duties.
    own=Project(name='职责测试',role='核心开发',description='团队提升性能。负责核心功能实现、联调。',achievements=['团队成果'])
    assert repeated_value(('project',own),'project.responsibilities')=='负责核心功能实现、联调。'
    own.description='团队提升性能。';own.role='核心开发'
    assert repeated_value(('project',own),'project.responsibilities')==''
    own.role='个人项目'
    assert repeated_value(('project',own),'project.responsibilities')==own.description
    own.responsibilities='用户明确确认的职责'
    assert repeated_value(('project',own),'project.responsibilities')==own.responsibilities
    # Location selection is grounded in this named internship, never the
    # candidate's residence or another employer. Only exact/suffix-equivalent
    # options or proven region paths are allowed.
    profile.location='上海';profile.internships[1].location='北京'
    location=field('w','experience.location');location.field_type='combobox';location.options=['北京市','上海市']
    places=snap([field('w','experience.organization','乙公司'),location])
    a=next(a for a in _local_safe_plan(places,profile).actions if a.selector==location.selector)
    assert a.action=='select' and a.value=='北京市'
    location.region_picker=True
    a=next(a for a in _local_safe_plan(places,profile).actions if a.selector==location.selector)
    assert a.action=='select' and a.value=='北京'
    places.fields[0].current_value='甲公司'
    assert next(a for a in _local_safe_plan(places,profile).actions if a.selector==location.selector).action=='ask_user'
    print("repeated_records_test: OK")


if __name__ == "__main__":
    run()
