"""A textarea is prose, not an unordered set of comma/newline options."""
from app.browser_models import BrowserSnapshot, PageField, FillAction, FormPlan
from app.form_agent import build_form_review


def compare(actual, expected, kind="textarea"):
    snapshot = BrowserSnapshot(session_id="fixture", url="https://example.test", title="form", fields=[
        PageField(selector="#description", label="项目描述", field_type=kind, current_value=actual),
        PageField(selector="#add", label="增加项目经历", field_type="section-button")])
    plan = FormPlan(actions=[FillAction(selector="#description", label="项目描述", action="fill",
        value=expected, value_source="verified fixture", confidence=1)])
    review = build_form_review(snapshot, plan)
    assert len(review.comparisons) == 1
    return review


def run():
    r = compare("开发服务，验证工具。 获得成果。", "开发服务，验证工具。\n获得成果。")
    assert r.summary.matched == 1 and r.plan.actions[0].action == "skip"
    assert compare("开发服务，获得成果。", "开发服务，验证工具。\n获得成果。").summary.conflict == 1
    assert compare("乙，甲", "甲，乙").summary.conflict == 1
    assert compare("C++", "C#", "text").summary.conflict == 1
    assert compare("乙, 甲", "甲, 乙", "combobox").summary.matched == 1
    print("form_review_text_test: OK (prose whitespace, no reorder/content loss, option semantics, no button questions)")


if __name__ == "__main__":
    run()
