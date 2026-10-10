"""Offline read-evidence merge: no browser, model, network or employer writes."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agents import ModelSettings, RunContextWrapper

from app import form_agent
from app.browser_models import BrowserSnapshot, FormPlan, PageField, QuestionEvidence
from app.browser_service import BrowserDemoService
from app.form_observation import (annotate_fields, assess_field, build_report,
                                  merge_observed_metadata, model_page, retain_adapter_evidence)
from app.models import CandidateProfile


def fixture():
    region = PageField(selector="#region", label="户口所在地", question_text="户口所在地",
        label_source="explicit", field_type="combobox", control_kind="select",
        options_capture="deferred", container_key="personal", current_value="本地已有内容")
    name = PageField(selector="#name", label="姓名", question_text="姓名",
        label_source="explicit", container_key="personal")
    fields = annotate_fields([region, name])
    snapshot = BrowserSnapshot(session_id="fixture", url="https://fixture.example.test/form",
        title="Fixture", fields=fields, extraction_report=build_report(fields, {
            "observed_controls": 3, "captured_controls": 2, "embedded_regions": 1,
            "pending_sections": ["证书"],
        }))
    sample = region.model_copy(deep=True, update={"options": ["北京市", "上海市"],
        "options_capture": "observed_subset", "region_picker": True,
        "region_value_path": "北京市 / 北京市 / 西城区"})
    return snapshot, sample


def merge_quality():
    snapshot, sample = fixture()
    # A read callback cannot import personal values or change control identity.
    sample.current_value = "不应复制的内容"
    sample.question_text = "不应改变的题目"
    merge_observed_metadata(snapshot, [sample])
    merged = snapshot.fields[0]
    assert merged.current_value == "本地已有内容" and merged.question_text == "户口所在地"
    assert merged.region_picker and merged.region_value_path == "北京市 / 北京市 / 西城区"
    assert merged.observation.options_status == "dependent"
    question = model_page(snapshot)["questions"][0]
    assert question["options_status"] == "dependent"
    report = snapshot.extraction_report
    assert report.options_pending_questions == 1 and report.question_count == 2
    assert report.unmapped_controls == 1 and report.embedded_regions == 1
    assert report.pending_sections == ["证书"] and report.capture_status == "partial"
    sample.options.append("fixture mutation")
    assert "fixture mutation" not in merged.options

    # A stale observer summary must not conceal newly contradictory evidence.
    conflicting = merged.model_copy(deep=True, update={"question_candidates": [
        QuestionEvidence(text="户口所在地", source="explicit", owned=True),
        QuestionEvidence(text="现居住地", source="explicit", owned=True),
    ]})
    merge_observed_metadata(snapshot, [conflicting])
    assert snapshot.fields[0].observation.question_status == "ambiguous"
    assert snapshot.extraction_report.unclear_questions == 1

    # Re-annotate every choice member, including read-only prompt context.
    yes = PageField(selector="#yes", label="是否接受调剂", question_text="是否接受调剂",
        label_source="explicit", field_type="radio", option_label="是",
        control_group_key="choice", container_key="one", options=["是", "否"],
        options_capture="group_complete")
    no = yes.model_copy(deep=True, update={"selector": "#no", "option_label": "否"})
    full = BrowserSnapshot(session_id="fixture", url=snapshot.url, title="Fixture", fields=[yes, no])
    annotate_fields(full.fields)
    full.extraction_report = build_report(full.fields, {"observed_controls": 2, "captured_controls": 2})
    filtered = full.model_copy(deep=True, update={"fields": [yes], "context_fields": [no]})
    report_before = filtered.extraction_report.model_dump()
    merge_observed_metadata(filtered, [yes], refresh=False)
    assert filtered.fields[0].observation.options_status == "group_complete"
    assert filtered.context_fields[0].observation.options_status == "group_complete"
    assert filtered.extraction_report.model_dump() == report_before


def record_and_required_quality():
    # A generic single-question marker cannot prove a shared education record.
    field = PageField(selector="#school", label="学校", question_text="学校",
        label_source="explicit", semantic_key="education.school",
        container_key="zhida-container-123-4")
    assert assess_field(field).record_status == "unresolved"
    for key in ("autohome:education:1:1", "phoenix-record-1", "ant-resume-fixture-1", "education:fixture-1"):
        observed = field.model_copy(update={"container_key": key})
        assert assess_field(observed).record_status == "container_observed"
    assert assess_field(field.model_copy(update={"container_key": "single-question"})).record_status == "unresolved"
    # Explicit DOM adapter proof can establish an otherwise opaque record ID.
    proved = field.model_copy(update={"container_key": "adapter-owned-record", "record_evidence": "ant-resume-owned"})
    assert assess_field(proved).record_status == "container_observed"
    snapshot = BrowserSnapshot(session_id="fixture", url="https://fixture.example.test/form",
        title="Fixture", fields=annotate_fields([field]))
    observed = field.model_copy(update={"record_evidence": "ant-resume-owned"})
    merge_observed_metadata(snapshot, [observed])
    # A generic scanner marker is not upgraded just by a source string.
    assert snapshot.fields[0].observation.record_status == "unresolved"
    assert model_page(snapshot)["questions"][0]["record_context"]["record_evidence"] == "ant-resume-owned"

    patch_value = retain_adapter_evidence({"required": True,
        "required_evidence": ["native:required", "aria-required=true"]},
        {"label_source": "container-owned", "question_text": "学校", "required": False}, "fixture-owned")
    assert patch_value["required"] is True
    assert patch_value["required_evidence"][:2] == ["native:required", "aria-required=true"]
    conflicted = field.model_copy(update={"required": patch_value["required"],
                                          "required_evidence": patch_value["required_evidence"]})
    assert "required_conflict" in assess_field(conflicted).issues
    assert assess_field(conflicted).required_status == "required"
    snapshot.fields = annotate_fields([conflicted])
    question = model_page(snapshot)["questions"][0]
    assert question["required"] and question["required_evidence"][:2] == ["native:required", "aria-required=true"]
    assert any("证据冲突" in issue for issue in question["issues"])
    # An adapter may positively discover a marker not captured by native HTML.
    positive = retain_adapter_evidence({"required": False, "required_evidence": []},
        {"label_source": "container-owned", "question_text": "学校", "required": True}, "fixture-owned")
    assert positive["required"] and not any(e.startswith("必填证据冲突：") for e in positive["required_evidence"])


async def sdk_merge():
    snapshot, sample = fixture()
    # An unmapped but owned caption reaches the semantic mapper; the known
    # 户口所在地 caption deliberately stays in the separate deterministic pass.
    snapshot.fields[0].label = snapshot.fields[0].question_text = "区域选项核对"
    sample.label = sample.question_text = "区域选项核对"
    annotate_fields(snapshot.fields)
    # Keep the callback's field identity stable, as the production guard demands.
    observer = AsyncMock(return_value=[sample])
    profile = CandidateProfile(name="匿名候选人", hukou_location="北京市西城区")

    async def fake_run(agent, prompt, max_turns):
        parsed = json.loads(prompt)
        assert [f["selector"] for f in parsed["page"]["fields"]] == ["#region"]
        tool = next(t for t in agent.tools if t.name == "inspect_controls")
        output = await tool.on_invoke_tool(RunContextWrapper(context=None),
            json.dumps({"selectors": ["#region"]}))
        assert json.loads(output)["observations"][0]["region_picker"]
        return SimpleNamespace(final_output=FormPlan())

    env = {"APP_AGENT_MODEL": "fixture", "APP_AGENT_FALLBACK_MODEL": "",
           "APP_AGENT_PROMPT_JSON_MODELS": ""}
    with patch.dict("os.environ", env), \
            patch.object(form_agent, "configured_model", return_value=("fixture", ModelSettings())), \
            patch.object(form_agent.Runner, "run", side_effect=fake_run):
        await form_agent.create_form_plan(snapshot, profile, observe_controls=observer)
    observer.assert_awaited_once_with(["#region"])
    assert snapshot.fields[0].region_picker and snapshot.fields[0].region_value_path
    assert snapshot.fields[0].observation.options_status == "dependent"
    assert snapshot.extraction_report.question_count == 2
    assert snapshot.extraction_report.embedded_regions == 1


async def inspect_merge():
    snapshot, sample = fixture()
    refreshed = snapshot.model_copy(deep=True)
    locator = SimpleNamespace(count=AsyncMock(return_value=1),
        scroll_into_view_if_needed=AsyncMock(), evaluate=AsyncMock())
    page = SimpleNamespace(locator=lambda selector: SimpleNamespace(first=locator),
                           bring_to_front=AsyncMock())
    service = BrowserDemoService()
    with patch.object(service, "_require", return_value=page), \
            patch.object(service, "snapshot", AsyncMock(side_effect=[snapshot, refreshed])), \
            patch.object(service, "observe_form_controls", AsyncMock(return_value=[sample])), \
            patch.object(service, "_assert_snapshot_document", AsyncMock()):
        result = await service.inspect_field("fixture", "#region")
    assert result.fields[0].region_picker and result.fields[0].region_value_path
    assert result.fields[0].observation.options_status == "dependent"
    assert result.extraction_report.options_pending_questions == 1
    assert result.extraction_report.embedded_regions == 1


async def run():
    merge_quality()
    record_and_required_quality()
    await sdk_merge()
    await inspect_merge()
    print("observation_metadata_merge_test: OK (hierarchy preserved; group/report synchronized; no writes)")


if __name__ == "__main__":
    asyncio.run(run())
