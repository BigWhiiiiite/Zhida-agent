"""Offline scanner-contract fixtures; no browser, model, user data or network.

The choice ownership shape follows the anonymous fieldsets in
backend/browser_smoke_test.py, and the separate upload/declaration shape follows
backend/ceb_question_ownership_test.py.  These fixtures are scanner-shaped data,
not evidence that a live employer page has been tested.
"""
from __future__ import annotations

import copy
import json

import pytest

from app.backend_v2.observation_adapter import adapt_browser_snapshot
from app.browser_models import BrowserSnapshot


def _field(selector: str, question: str, **extra):
    return {
        "selector": selector, "label": question, "question_text": question,
        "label_source": "explicit", "field_type": "text",
        "question_candidates": [{"text": question, "source": "explicit", "owned": True}],
        **extra,
    }


def snapshot_fixture():
    question = "除上述选择外，是否还接受其他城市分配"
    return {
        "session_id": "synthetic-local-session", "url": "https://synthetic.invalid/apply",
        "title": "Synthetic form", "browser_engine": "safari",
        "fields": [
            _field("#relocation-yes", question, field_type="radio", control_kind="radio",
                   control_group_key="owned-relocation", options=["是", "否"],
                   option_label="是", option_value="yes", options_capture="group_complete",
                   required=True, required_evidence=["当前题目标签的必填标记"]),
            _field("#relocation-no", question, field_type="radio", control_kind="radio",
                   control_group_key="owned-relocation", options=["是", "否"],
                   option_label="否", option_value="no", options_capture="group_complete"),
            _field("#city", "期望工作城市", field_type="combobox", control_kind="select",
                   options=[], options_capture="deferred", current_value="测试私有城市"),
            _field("#date", "开始日期", field_type="combobox", control_kind="calendar",
                   date_precision="month", control_evidence="scanner-owned-calendar"),
            _field("#photo", "个人照片", field_type="file", accept="image/png,image/jpeg",
                   help_text="上传文件大小不能超过3M"),
            _field("#master-school", "学校名称", semantic_key="education.school",
                   container_key="education:master-record", record_evidence="ant-resume-owned",
                   entity_scope="education:master", current_value="合成研究生大学",
                   section_path=["教育经历", "硕士教育经历"]),
            _field("#bachelor-school", "学校名称", semantic_key="education.school",
                   container_key="education:bachelor-record", record_evidence="ant-resume-owned",
                   entity_scope="education:bachelor", current_value="合成本科大学",
                   section_path=["教育经历", "本科教育经历"]),
        ],
        "context_fields": [_field("#context-only", "附近文字", label_source="nearby",
                                  question_candidates=[])],
        "extraction_report": {
            "capture_status": "partial", "observed_controls": 8, "captured_controls": 7,
            "unmapped_controls": 1, "embedded_regions": 1, "unread_shadow_regions": 1,
            "pending_sections": ["实习经历（有新增入口，记录尚未呈现）"],
            "limitations": ["仅代表已呈现的当前文档"],
        },
    }


def test_real_browser_model_and_json_have_identical_adapter_contract():
    source = snapshot_fixture()
    result = adapt_browser_snapshot(source)
    assert result == adapt_browser_snapshot(BrowserSnapshot.model_validate(source))
    assert result["fields"][0]["question_text"] == "除上述选择外，是否还接受其他城市分配"
    assert result["fields"][0]["required_evidence"] == ["当前题目标签的必填标记"]
    assert result["fields"][0]["options"] == ["是", "否"]
    assert result["fields"][2]["options_capture"] == "deferred"
    assert result["fields"][2]["options"] == []
    assert result["fields"][3]["control_kind"] == "calendar"
    assert result["fields"][3]["date_precision"] == "month"
    assert result["fields"][4]["accept"] == "image/png,image/jpeg"
    assert len(result["fields"]) == 7 and len(result["context_fields"]) == 1
    assert result["extraction_report"]["capture_status"] == "partial"
    assert result["extraction_report"]["embedded_regions"] == 1


def test_adapter_does_not_mutate_evidence_or_erase_question_conflicts():
    source = snapshot_fixture()
    source["fields"][0]["question_candidates"].append(
        {"text": "是否可以提前实习", "source": "aria-labelledby", "owned": True})
    before = copy.deepcopy(source)
    result = adapt_browser_snapshot(source)
    assert len(result["fields"][0]["question_candidates"]) == 2
    result["fields"][0]["question_candidates"][0]["text"] = "changed-output"
    result["extraction_report"]["pending_sections"].clear()
    assert source == before


def test_only_narrow_record_anchor_values_survive_as_sensitive_evidence():
    source = snapshot_fixture()
    source["fields"].extend([
        _field("#name", "姓名", semantic_key="candidate.name", current_value="私有姓名"),
        _field("#email", "邮箱", current_value="private@example.invalid"),
        _field("#spoof-password", "密码", semantic_key="education.school",
               field_type="password", current_value="secret-password"),
        _field("#spoof-otp", "验证码", semantic_key="education.school",
               current_value="secret-code"),
        _field("#hidden", "学校名称", semantic_key="education.school",
               field_type="hidden", current_value="secret-hidden"),
    ])
    result = adapt_browser_snapshot(source)
    by_id = {field["selector"]: field for field in result["fields"]}
    assert by_id["#master-school"]["current_value"] == "合成研究生大学"
    assert by_id["#bachelor-school"]["current_value"] == "合成本科大学"
    serialized = json.dumps(result, ensure_ascii=False)
    for value in ("私有姓名", "private@example.invalid", "secret-password", "secret-code",
                  "secret-hidden", "测试私有城市"):
        assert value not in serialized


def test_extras_cannot_inject_images_model_answers_or_screenshot_title_proof():
    source = snapshot_fixture()
    source.update({"image_data_url": "data:image/png;base64,private-image",
                   "candidate_profile": {"name": "private-profile"},
                   "knowledge_context": [], "cookies": "private-cookie"})
    source["fields"][0].update({
        "knowledge_profile_path": "unverified-profile-path", "knowledge_id": "private-memory",
        "image_data_url": "private-field-image", "ocr_text": "fabricated-title",
        "question_candidates": [{"text": "原题", "source": "nearby", "owned": False}],
    })
    result = adapt_browser_snapshot(source)
    assert result["fields"][0]["question_candidates"] == [
        {"text": "原题", "source": "nearby", "owned": False}]
    serialized = json.dumps(result, ensure_ascii=False)
    for value in ("private-image", "private-profile", "private-cookie", "private-memory",
                  "private-field-image", "fabricated-title", "unverified-profile-path"):
        assert value not in serialized


@pytest.mark.parametrize("evidence", [
    [{"text": "学校名称", "source": "nearby", "owned": False}],
    [{"text": "学校名称", "source": "model", "owned": True}],
    [{"text": "学校名称", "source": "explicit", "owned": True},
     {"text": "证件号码", "source": "aria", "owned": True}],
])
def test_unverified_anchor_question_cannot_retain_existing_personal_value(evidence):
    source = snapshot_fixture()
    source["fields"][5]["question_candidates"] = evidence
    result = adapt_browser_snapshot(source)
    assert "current_value" not in result["fields"][5]
    assert result["fields"][5]["question_candidates"] == evidence


def test_cascade_disabled_option_metadata_survives_without_claiming_complete_options():
    source = snapshot_fixture()
    source["fields"][2].update({
        "control_kind": "cascade", "options_capture": "dependent",
        "cascade_observation": {
            "observed_layer_count": 1, "truncated": False,
            "layers": [{"visible_layer_index": 0, "visible_option_count": 2,
                        "truncated": False, "options": [
                            {"text": "可用地区", "disabled": False, "branch": True},
                            {"text": "不可用地区", "disabled": True, "branch": False},
                        ]}],
        },
    })
    result = adapt_browser_snapshot(source)
    cascade = result["fields"][2]["cascade_observation"]
    assert cascade["layers"][0]["options"][1]["disabled"] is True
    assert cascade["complete"] is False
    assert result["fields"][2]["options_capture"] == "dependent"


def test_explicit_typed_option_evidence_preserves_disabled_and_never_invents_values():
    source = snapshot_fixture()
    source["fields"][2].update({
        "disabled": True, "field_type": "select-one", "control_kind": "native_select",
        "options_capture": "native_complete", "options": [
            {"label": "北京", "value": "BJ", "disabled": False},
            {"text": "上海", "disabled": True}, "未记录代码的城市",
        ],
    })
    before = copy.deepcopy(source)
    result = adapt_browser_snapshot(source)
    assert result["fields"][2]["disabled"] is True
    assert result["fields"][2]["options"] == [
        {"label": "北京", "value": "BJ", "disabled": False},
        {"label": "上海", "disabled": True}, "未记录代码的城市",
    ]
    assert source == before


def test_invalid_disabled_values_are_rejected_not_silently_enabled():
    source = snapshot_fixture()
    source["fields"][2]["disabled"] = "false"
    with pytest.raises(ValueError, match="disabled state"):
        adapt_browser_snapshot(source)
    source["fields"][2].pop("disabled")
    source["fields"][2]["options"] = [{"label": "北京", "disabled": "true"}]
    with pytest.raises(ValueError, match="disabled state"):
        adapt_browser_snapshot(source)


@pytest.mark.parametrize("value", [float("nan"), object(), {1: "bad-key"}])
def test_invalid_json_evidence_is_rejected_without_echoing_raw_values(value):
    source = snapshot_fixture()
    source["extras"] = value
    with pytest.raises(ValueError, match="observation"):
        adapt_browser_snapshot(source)


def test_invalid_scanner_schema_errors_do_not_echo_applicant_values():
    source = snapshot_fixture()
    source["fields"][0]["field_type"] = {"applicant": "private-value"}
    with pytest.raises(ValueError) as error:
        adapt_browser_snapshot(source)
    assert str(error.value) == "invalid browser observation evidence schema"
    assert "private-value" not in str(error.value)


def test_oversized_or_recursive_evidence_is_rejected_not_truncated():
    source = snapshot_fixture()
    source["fields"] = [_field(f"#f{i}", "原题") for i in range(1001)]
    with pytest.raises(ValueError, match="bounded evidence limit"):
        adapt_browser_snapshot(source)
    source = snapshot_fixture()
    source["fields"][0]["context"] = "x" * 65_537
    with pytest.raises(ValueError, match="bounded evidence limit"):
        adapt_browser_snapshot(source)
    source = snapshot_fixture()
    source["loop"] = source
    with pytest.raises(ValueError, match="bounded evidence limit"):
        adapt_browser_snapshot(source)
