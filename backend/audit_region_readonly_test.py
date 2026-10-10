"""Anonymous masked audit views; no live browser, model, dotenv or employer writes."""
import ast
import asyncio
import base64
import copy
import inspect
import io
import json
import textwrap
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image
from agents import ModelSettings

from app import extraction_audit as audit
from app.browser_models import BrowserSnapshot, PageField
from app.browser_service import BrowserDemoService
from app.page_observation import REGION_SCRIPT, PageRegionObservation


MASK_SELECTOR = ('input,textarea,select,[role="textbox"],[role="combobox"],'
                 '[contenteditable="true"],img,canvas,iframe,video,[role="checkbox"],[role="radio"]')
FORBIDDEN = {"click", "dblclick", "fill", "check", "uncheck", "select_option",
             "set_input_files", "press", "type", "goto", "reload", "submit", "save"}


class AnonymousPage:
    def __init__(self, selector, caption):
        self.url = "https://fixture.example.test/form"
        self.selector = selector
        self.requests = []
        self.target = SimpleNamespace(scroll_into_view_if_needed=AsyncMock(),
            aria_snapshot=AsyncMock(return_value='- textbox "' + caption + '"'))
        self.mask = SimpleNamespace()
        region = {
            "crop": {"x": 0, "y": 0, "width": 100, "height": 60},
            "viewport": {"width": 100, "height": 60},
            "masks": [{"x": 10, "y": 20, "width": 80, "height": 30}],
            "redactions": [], "context": {"labels": [caption],
                "accessible_nodes": [{"role": "input", "name": caption}], "context_only": True},
            "document_stamp": [self.url, 1, 0, 0, 100, 60],
            "target_stamp": ["INPUT", "fixture", "text", "", {"x": 10, "y": 20}, "anonymous"],
        }
        self.evaluate = AsyncMock(side_effect=lambda script, target: copy.deepcopy(region))
        output = io.BytesIO()
        Image.new("RGB", (100, 60), "white").save(output, format="PNG")
        self.screenshot = AsyncMock(return_value=output.getvalue())
        self.forbidden = {name: AsyncMock(side_effect=AssertionError("No employer mutation")) for name in FORBIDDEN}
        for name, method in self.forbidden.items():
            setattr(self, name, method)
            setattr(self.target, name, method)

    def locator(self, selector):
        self.requests.append(selector)
        if selector == self.selector:
            return self.target
        if selector == MASK_SELECTOR:
            return self.mask
        raise AssertionError("Unapproved locator")

    def assert_no_writes(self):
        for method in self.forbidden.values():
            method.assert_not_awaited()


def snapshot(field):
    return BrowserSnapshot(session_id="fixture", url="https://fixture.example.test/form",
                           title="Anonymous", fields=[field])


async def allowed_masked(field):
    page = AnonymousPage(field.selector, field.question_text)
    before = snapshot(field)
    service = BrowserDemoService()
    with patch.object(service, "_require", return_value=page), \
            patch.object(service, "snapshot", AsyncMock(return_value=before)), \
            patch.object(service, "_assert_snapshot_document", AsyncMock()):
        result = await service.observe_page_region("fixture", field.selector,
            include_image=True, bring_into_view=True, audit_only=True, expected_snapshot=before)
    assert result.read_only and result.selector == field.selector
    assert result.image_data_url.startswith("data:image/png;base64,")
    image_bytes = base64.b64decode(result.image_data_url.split(",", 1)[1])
    with Image.open(io.BytesIO(image_bytes)) as image:
        # Exercise the real masker, not a mocked promise to hide controls.
        assert image.getpixel((20, 30)) == (100, 116, 108)
        assert image.getpixel((1, 1)) == (255, 255, 255)
    page.target.scroll_into_view_if_needed.assert_awaited_once_with(timeout=3000)
    for call in page.evaluate.await_args_list:
        assert call.args == (REGION_SCRIPT, field.selector)
    kwargs = page.screenshot.await_args.kwargs
    assert kwargs["mask"] == [page.mask] and kwargs["mask_color"] == "#64746c"
    page.assert_no_writes()


async def denied(field, *, audit_only):
    page = AnonymousPage(field.selector, field.question_text)
    service = BrowserDemoService()
    before = snapshot(field)
    with patch.object(service, "_require", return_value=page), \
            patch.object(service, "snapshot", AsyncMock(return_value=before)), \
            patch.object(service, "_assert_snapshot_document", AsyncMock()):
        try:
            await service.observe_page_region("fixture", field.selector,
                include_image=True, bring_into_view=True, audit_only=audit_only)
        except ValueError:
            pass
        else:
            raise AssertionError("Forbidden target reached visual observation")
    page.target.scroll_into_view_if_needed.assert_not_awaited()
    page.evaluate.assert_not_awaited()
    page.screenshot.assert_not_awaited()
    assert not page.requests
    page.assert_no_writes()


async def stale_read_stops(field):
    page = AnonymousPage(field.selector, field.question_text)
    before = snapshot(field)
    changed = before.model_copy(deep=True)
    changed.fields[0].current_value = "fixture-changed"
    service = BrowserDemoService()
    with patch.object(service, "_require", return_value=page), \
            patch.object(service, "snapshot", AsyncMock(side_effect=[before, changed])), \
            patch.object(service, "_assert_snapshot_document", AsyncMock()):
        try:
            await service.observe_page_region("fixture", field.selector,
                include_image=True, audit_only=True)
        except ValueError as exc:
            assert "已丢弃结果" in str(exc)
        else:
            raise AssertionError("Changed declaration state accepted")
    page.assert_no_writes()


async def control_credentials_gate():
    # Audit menu discovery shares this gate with ordinary mapper tools.
    # A text-looking/custom-select OTP is still a credential via autocomplete.
    field = PageField(selector="#code", question_text="输入项", label="输入项",
        field_type="combobox", autocomplete="one-time-code")
    page = AnonymousPage(field.selector, field.question_text)
    service = BrowserDemoService()
    service.page = page
    before = snapshot(field)
    state = SimpleNamespace(stage="application_form", navigation_blocker="")
    with patch.object(service, "workflow_state", AsyncMock(return_value=state)), \
            patch.object(service, "snapshot", AsyncMock(return_value=before)), \
            patch.object(service, "_assert_snapshot_document", AsyncMock()):
        try:
            await service.observe_form_controls("fixture", [field.selector])
        except ValueError:
            pass
        else:
            raise AssertionError("OTP autocomplete reached option discovery")
    assert not page.requests
    page.screenshot.assert_not_awaited()
    page.evaluate.assert_not_awaited()
    page.assert_no_writes()


async def runner_receives_sanitized_regions():
    """Check the actual audit-to-Runner boundary, not just the sanitizer alone."""
    source = BrowserSnapshot(session_id="fixture", url="https://fixture.example.test/form",
        title="Anonymous", fields=[
            PageField(selector="#file-review", field_type="file", current_value="ANON-FILE-VALUE.pdf"),
            PageField(selector="#declaration-review", label="本人承诺资料真实可信",
                question_text="本人承诺资料真实可信", label_source="explicit",
                field_type="combobox", current_value="是", options_capture="deferred"),
        ])
    source_before = source.model_dump()
    originals = {}
    seen_regions = []

    async def observe_region(selector):
        assert selector in {f.selector for f in source.fields}
        sample = PageRegionObservation(selector=selector, accessibility_source="playwright_aria",
            accessibility='- checkbox "本人承诺资料真实可信" [checked]\n'
                '- option "确认" [selected]\n- button "确认" [pressed]\n'
                '- spinbutton "评级" [valuenow=7] [valuetext=七]\n'
                '- textbox "备注: 内容": 7\n- searchbox "简称": 男\n- combobox "确认选择": 是',
            context={"labels": ["ANON-FILE-VALUE.pdf"],
                "nested": {"help": "ANON-FILE-VALUE.pdf"}},
            image_data_url="data:image/png;base64,YQ==")
        originals[selector] = sample
        return sample

    async def fake_run(agent, messages, max_turns):
        assert not agent.tools and agent.output_type == audit.AuditBatch and max_turns == 1
        parts = messages[0]["content"]
        page = json.loads(parts[0]["text"])
        for part in parts[1:]:
            if part["type"] != "input_text":
                assert part["type"] == "input_image" and part["image_url"] == "data:image/png;base64,YQ=="
                continue
            sample = json.loads(part["text"])
            seen_regions.append(sample["selector"])
            text = sample["accessibility"]
            assert "本人承诺资料真实可信" in text and '- option "确认"' in text
            assert '- textbox "备注: 内容"' in text, "A colon inside a caption is not a user value"
            assert all(tag not in text for tag in ("[checked]", "[selected]", "[pressed]",
                "[valuenow", "[valuetext"))
            assert all(value not in text for value in (": 7", ": 男", ": 是"))
            assert text.count("[填写值已遮挡]") == 3
            assert "ANON-FILE-VALUE.pdf" not in part["text"]
            assert "image_data_url" not in sample
        return SimpleNamespace(final_output=audit.AuditBatch(questions=[
            audit.QuestionReview(question_id=q["id"], interpretation=q["question_text"] or "原题尚未读清",
                control_kind=q["control_kind"], verdict="needs_observation" if q["issues"] else "clear",
                issue="；".join(q["issues"]), next_observation="核对原题",
                evidence=[q["question_text"]] if q["question_text"] else []) for q in page["questions"]]))

    with patch.dict("os.environ", {"APP_AGENT_MODEL": "fixture", "APP_AGENT_PROMPT_JSON_MODELS": ""}), \
            patch.object(audit, "configured_model", return_value=("fixture", ModelSettings())), \
            patch.object(audit.Runner, "run", side_effect=fake_run):
        result = await audit.audit_extraction(source,
            audit.ExtractionAuditRequest(context_token="x" * 64, include_images=True),
            observe_region=observe_region)
    assert result.model_status == "complete" and result.model_reviewed_questions == 2
    assert set(seen_regions) == {"#file-review", "#declaration-review"}
    assert result.images_supplied == 2 and "image_data_url" not in result.model_dump_json()
    assert source.model_dump() == source_before
    assert all("[checked]" in sample.accessibility for sample in originals.values())


def implementation_contract():
    tree = ast.parse(textwrap.dedent(inspect.getsource(BrowserDemoService.observe_page_region)))
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls.intersection(FORBIDDEN), "Read-only region path has mutation capability"
    # Inspect source without importing main, which would load the user's env.
    main_tree = ast.parse((Path(__file__).parent / "app" / "main.py").read_text())
    endpoint = next(node for node in main_tree.body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == "extraction_audit")
    region_calls = [node for node in ast.walk(endpoint) if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute) and node.func.attr == "observe_page_region"]
    assert len(region_calls) == 1
    kwargs = {k.arg: k.value for k in region_calls[0].keywords}
    assert all(isinstance(kwargs[name], ast.Constant) and kwargs[name].value is True
               for name in ("include_image", "bring_into_view", "audit_only"))


async def run():
    file = PageField(selector="#file", label="简历附件", question_text="简历附件",
        field_type="file", accept=".pdf", label_source="explicit")
    declaration = PageField(selector="#declaration", label="本人承诺资料真实可信",
        question_text="本人承诺资料真实可信", field_type="combobox", label_source="explicit")
    for field in (file, declaration):
        await allowed_masked(field)
        await denied(field, audit_only=False)
    for field in (
        PageField(selector="#password", field_type="password"),
        PageField(selector="#otp", label="手机验证码", field_type="text"),
        PageField(selector="#hidden", field_type="hidden"),
        PageField(selector="#add", field_type="section-button"),
        PageField(selector="#autofill-code", field_type="text", autocomplete="one-time-code"),
    ):
        await denied(field, audit_only=True)
    await stale_read_stops(declaration)
    await control_credentials_gate()
    await runner_receives_sanitized_regions()
    implementation_contract()
    assert "one-time-code" in REGION_SCRIPT, "OTP values must be excluded even during masking"
    print("audit_region_readonly_test: OK (masked file/declaration audit; mapper denied; no writes/credentials; sanitized Runner input)")


if __name__ == "__main__":
    asyncio.run(run())
