"""Exercise built React in a fresh browser, with anonymous intercepted APIs only.

Usage: backend/.venv/bin/python frontend/src/simpleApplicationFlow.ui_test.py [dist]
No server, real account, browser profile, model, or recruitment website is used.
Every unexpected HTTP request and WebSocket makes the test fail.
"""
import asyncio
import mimetypes
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.application_models import ApplicationWorkflowState  # noqa: E402
from app.browser_models import (  # noqa: E402
    ApplicationAssistResult, BrowserSnapshot, FieldObservation, FillAction,
    FormExtractionReport, FormPlan, FormReviewResult, FormRoutingSummary,
    PageField, PreSubmitCheck, RequiredFieldIssue,
)
from app.models import CandidateProfile, ResumeProfile, ResumeRecord  # noqa: E402
from playwright.async_api import async_playwright, expect  # noqa: E402

# A synthetic HTTPS origin supplies the secure-context UUID API used for receipts.
ORIGIN = "https://zhida-ui.invalid"
URL = "https://ats-fixture.example.test/application?job=anonymous"
SID = "00000000-0000-4000-8000-000000000123"
RID = "anonymous-cv"
QUESTION = "你是否接受异地工作？"
AUDIT_BUTTON = "只读检查整页识别（不填写）"
SOURCE_QUESTION = "招聘信息来源"
TECHNOLOGY_QUESTION = "关注的技术方向"
SOURCE_OPTIONS = [f"匿名来源{index + 1}" for index in range(14)]
TECHNOLOGY_OPTIONS = ["匿名算法", "匿名开发", "匿名测试"]


def anonymous_fixtures(*, checkboxes=False):
    now = datetime.now(timezone.utc)
    profile = CandidateProfile(name="匿名测试")
    resume = ResumeRecord(id=RID, filename="anonymous.pdf", label="匿名测试简历",
                          parser="fixture", profile=ResumeProfile(), is_default=True,
                          status="completed", created_at=now, updated_at=now)
    missing = PageField(selector="#unread", label="未识别字段1", required=True,
                        label_source="generated", field_type="text",
                        observation=FieldObservation(question_status="missing", issues=["question_missing"]))
    dropdown = PageField(selector="#city", label="到岗城市", question_text="你的可到岗城市是？",
                         required=True, field_type="select-one", label_source="label",
                         control_kind="native_select", recognition_confidence=1,
                         observation=FieldObservation(question_status="verified", options_status="unavailable"))
    radios = [PageField(selector=f"#mobility-{value}", label=label, question_text=QUESTION,
                        label_source="fieldset", recognition_confidence=1, field_type="radio",
                        required=True, name="mobility", group_label=QUESTION,
                        control_group_key="fixture-mobility", option_label=label, option_value=value,
                        options=["是", "否"], semantic_key="application.mobility", control_kind="radio",
                        observation=FieldObservation(question_status="verified", options_status="group_complete"))
              for value, label in [("yes", "是"), ("no", "否")]]
    fields = [missing, dropdown, *radios]
    if checkboxes:
        for key, title, options, required, selected in (
                ("sources", SOURCE_QUESTION, SOURCE_OPTIONS, True, 2),
                ("technology", TECHNOLOGY_QUESTION, TECHNOLOGY_OPTIONS, False, 1)):
            fields.extend(PageField(selector=f"#{key}-{index}", label=option, question_text=title,
                                    group_label=title, option_label=option, option_value=f"{key}-{index}",
                                    # Shared HTML names must not merge independent DOM questions.
                                    name="anonymous-shared-name", field_type="checkbox", multiple=False,
                                    options=options, required=required, current_value="true" if index == selected else "false",
                                    control_group_key=f"fixture-{key}", container_key=f"fixture-{key}-owner",
                                    semantic_key=f"application.{key}", entity_scope="application",
                                    field_signature=f"checkbox-option-v1:fixture-{key}-{index}",
                                    control_kind="checkbox", label_source="container-owned", recognition_confidence=1,
                                    observation=FieldObservation(question_status="verified", options_status="group_complete"))
                          for index, option in enumerate(options))
    report = FormExtractionReport(capture_status="partial", observed_controls=4, captured_controls=4,
                                  question_count=3, verified_questions=2, unclear_questions=1,
                                  options_pending_questions=1)
    if checkboxes:
        report.observed_controls = report.captured_controls = len(fields)
        report.question_count += 2
        report.verified_questions += 2
    snapshot = BrowserSnapshot(session_id=SID, url=URL, title="匿名信息填写页",
                               browser_engine="safari", fields=fields, extraction_report=report)
    workflow = ApplicationWorkflowState(session_id=SID, url=URL, title=snapshot.title,
                                         stage="application_form", authenticated=True,
                                         form_fields=len(fields), final_submit_present=True)
    plan = FormPlan(context_token="f" * 64, resume_id=RID, page_summary="合成识别结果",
                    actions=[FillAction(selector=field.selector, label=field.question_text or field.label,
                                        action="ask_user", confidence=1, reason="匿名夹具待处理",
                                        resolution_source="user") for field in fields],
                    routing_summary=FormRoutingSummary(needs_user=4))
    review = FormReviewResult(snapshot=snapshot, plan=plan)
    check = PreSubmitCheck(url=URL, required_total=3, ready=False,
                           required_missing=[RequiredFieldIssue(selector=field.selector,
                                                                 label=field.question_text or field.label,
                                                                 field_type=field.field_type)
                                             for field in [missing, dropdown, radios[0]]],
                           submit_labels=["提交"])
    assisted = ApplicationAssistResult(status="needs_user", message="匿名一轮处理结果",
                                       snapshot=snapshot, review=review, pre_submit=check, rounds=1,
                                       events=[{"kind": "observe", "message": "合成页面已检查", "completed": 0, "failed": 0}])
    return now, profile, resume, snapshot, workflow, assisted


async def case(browser, dist, *, connected, mobile=False, fresh_bind=False, checkboxes=False):
    now, profile, resume, snapshot, workflow, assisted = anonymous_fixtures(checkboxes=checkboxes)
    writes, reads, errors, unexpected = [], [], [], []
    state = {"run_id": "", "audit_count": 0, "connected": connected}
    headers = {"access-control-allow-origin": ORIGIN, "access-control-allow-credentials": "true",
               "access-control-allow-methods": "GET,POST,OPTIONS", "access-control-allow-headers": "content-type"}

    async def intercept(route):
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if parsed.hostname in {"fonts.googleapis.com", "fonts.gstatic.com"} and request.method == "GET":
            await route.fulfill(body="", content_type="text/css")
            return
        api_request = parsed.hostname == "zhida-ui.invalid" and parsed.port in {None, 8000} and path.startswith("/api/")
        if api_request:
            if request.method == "OPTIONS":
                await route.fulfill(status=204, headers=headers)
                return
            if request.method == "GET":
                reads.append(path)
            else:
                writes.append((request.method, path, request.post_data_json))
            body = None
            if request.method == "GET":
                if path == "/api/auth/me":
                    body = {"id": "anonymous", "email": "fixture@example.invalid", "display_name": "匿名测试",
                            "created_at": now.isoformat(), "is_local": True}
                elif path == "/api/profile": body = profile.model_dump(mode="json")
                elif path == "/api/resumes": body = [resume.model_dump(mode="json")]
                elif path in {"/api/conflicts", "/api/application-knowledge", "/api/application-knowledge/targets"}: body = []
                elif path == "/api/browser/current":
                    body = {"session_id": SID if state["connected"] else None, "resume_id": RID if state["connected"] else "",
                            "occupied": state["connected"], "assistance_version": 1, "record_completion_version": 1,
                            "journey_version": 1}
                elif state["connected"] and path == f"/api/browser/{SID}/snapshot": body = snapshot.model_dump(mode="json")
                elif state["connected"] and path == f"/api/browser/{SID}/workflow": body = workflow.model_dump(mode="json")
                elif state["connected"] and path == f"/api/browser/{SID}/observation-consent":
                    body = {"enabled": False, "context_token": "f" * 64}
                elif state["connected"] and state["run_id"] and path == f"/api/browser/{SID}/assist/progress/{state['run_id']}":
                    body = {"run_id": state["run_id"], "status": "finished", "phase": "needs_user",
                            "message": "匿名结果", "events": [], "result": assisted.model_dump(mode="json"),
                            "cancel_requested": False}
            elif fresh_bind and request.method == "POST" and path == "/api/websites/safari/preview-existing":
                assert request.post_data_json == {"url": URL}
                body = {"preview_token": "fixture-preview", "url": URL, "expires_in_seconds": 60}
            elif fresh_bind and request.method == "POST" and path == "/api/websites/safari/confirm-existing":
                assert request.post_data_json == {"url": URL, "preview_token": "fixture-preview"}
                body = {"status": "requested", "browser": "safari", "automation_connected": False,
                        "safari_window_token": "fixture-window", "window_reused": True}
            elif fresh_bind and request.method == "POST" and path == "/api/browser/start":
                assert request.post_data_json == {"url": URL, "resume_id": RID, "safari_window_token": "fixture-window"}
                state["connected"] = True
                body = snapshot.model_dump(mode="json")
            elif state["connected"] and request.method == "POST" and path == f"/api/browser/{SID}/assist":
                payload = request.post_data_json
                assert payload == {"resume_id": RID, "allow_site_parse": False, "use_model": True, "max_rounds": 5}, payload
                state["run_id"] = parse_qs(parsed.query).get("run_id", [""])[0]
                assert state["run_id"], "An explicit assist must have its own receipt UUID"
                body = assisted.model_dump(mode="json")
            elif checkboxes and state["connected"] and request.method == "POST" and path == f"/api/browser/{SID}/execute":
                # Stop at the captured payload: this fixture does not simulate
                # a successful website write, read-back or memory persistence.
                payload = request.post_data_json
                assert payload["resume_id"] == RID and payload["context_token"] == "f" * 64
                actions = payload["actions"]
                source_actions = [action for action in actions if action["selector"].startswith("#sources-")]
                assert len(source_actions) == len(SOURCE_OPTIONS)
                assert {action["selector"]: action["value"] for action in source_actions} == {
                    f"#sources-{index}": index == 0 for index in range(len(SOURCE_OPTIONS))}
                assert all(action["action"] == "check" and action["user_confirmed"] is True and
                           action["resolution_source"] == "user" for action in source_actions)
                assert all(action["action"] == "ask_user" for action in actions if not action["selector"].startswith("#sources-")), actions
                await route.fulfill(status=409, headers=headers, json={"detail": "匿名测试已截获复选题填写计划；没有执行网页或保存记忆。"})
                return
            elif state["connected"] and request.method == "POST" and path == f"/api/browser/{SID}/extraction-audit":
                assert request.post_data_json == {"context_token": "f" * 64, "use_model": True,
                                                  "inspect_controls": False, "include_images": False}
                state["audit_count"] += 1
                body = {"session_id": SID, "read_only": True, "scope": "current_visible_document",
                        "model_status": "partial", "model_name": "fixture-model", "total_questions": 3,
                        "model_reviewed_questions": 0, "model_batches": 1, "observations_requested": 0,
                        "observations_received": 0, "images_supplied": 0, "input_manifest": {},
                        "coverage": snapshot.extraction_report.model_dump(mode="json"),
                        "limitations": ["匿名测试未调用模型"], "questions": []}
            if body is None:
                unexpected.append((request.method, request.url))
                await route.abort()
                return
            await route.fulfill(headers=headers, json=body)
            return
        if parsed.hostname != "zhida-ui.invalid" or parsed.port is not None or request.method != "GET":
            unexpected.append((request.method, request.url))
            await route.abort()
            return
        file = (dist / (path.lstrip("/") or "index.html")).resolve()
        if not file.is_relative_to(dist) or not file.is_file():
            unexpected.append((request.method, request.url))
            await route.abort()
            return
        await route.fulfill(body=file.read_bytes(), content_type=mimetypes.guess_type(file)[0] or "application/octet-stream")

    context = await browser.new_context(service_workers="block", viewport={"width": 390 if mobile else 1280, "height": 900})
    page = await context.new_page()
    page.set_default_timeout(5000)
    page.on("pageerror", lambda error: errors.append(str(error)))
    async def accept_dialog(dialog):
        await dialog.accept()
    page.on("dialog", accept_dialog)
    async def block_socket(socket):
        unexpected.append("WebSocket")
        await socket.close()
    await page.route_web_socket("**/*", block_socket)
    await page.route("**/*", intercept)

    async def assert_sections(active):
        for name in ("operation", "questions", "diagnostics"):
            panel = page.locator(f'[data-workspace-section="{name}"]')
            await expect(panel).to_have_count(1)
            if name == active:
                await expect(panel).to_be_visible()
                assert await panel.get_attribute("hidden") is None
            else:
                await expect(panel).to_be_hidden()
                assert await panel.get_attribute("hidden") is not None
                assert await panel.evaluate("element => getComputedStyle(element).display") == "none"

    async def enter_workspace():
        setup = page.locator(".application-setup")
        if await setup.is_visible():
            await setup.get_by_label("投递简历", exact=True).select_option(RID)
            await setup.get_by_label("信息填写页网址", exact=True).fill(URL)
            await expect(setup.get_by_role("button", name="确认并继续", exact=True)).to_be_enabled()
            await setup.get_by_role("button", name="确认并继续", exact=True).click()
        await assert_sections("operation")

    try:
        await page.goto(ORIGIN + "/")
        await page.get_by_role("button", name="投递工作台", exact=True).click()
        if not connected:
            setup = page.locator(".application-setup")
            await expect(setup).to_be_visible()
            await expect(setup.get_by_label("投递简历", exact=True)).to_be_enabled()
            await expect(setup.locator("select")).to_have_count(1)
            await expect(setup.locator("input")).to_have_count(1)
            await expect(setup.get_by_role("button")).to_have_count(1)
            await expect(setup.get_by_role("button", name="确认并继续", exact=True)).to_be_disabled()
            assert not writes, writes
            assert await page.get_by_role("button", name=AUDIT_BUTTON, exact=True).count() == 0
            await setup.get_by_label("投递简历", exact=True).select_option(RID)
            await setup.get_by_label("信息填写页网址", exact=True).fill("javascript:alert(1)")
            await expect(setup.get_by_role("button", name="确认并继续", exact=True)).to_be_disabled()
            await enter_workspace()
            assert not writes, "Confirming the setup must not navigate, fill, or call a model"
            await expect(page.get_by_role("button", name="投递（辅助填写）", exact=True)).to_be_disabled()
            await expect(page.get_by_role("button", name="连接我已登录的 Safari 窗口", exact=True)).to_be_visible()
            if not fresh_bind:
                await page.get_by_role("button", name="返回设置", exact=True).click()
                await expect(setup.get_by_label("信息填写页网址", exact=True)).to_have_value(URL)
                assert not writes, "Back to setup is a local UI action"
            else:
                await page.get_by_role("button", name="连接我已登录的 Safari 窗口", exact=True).click()
                await expect(page.get_by_role("button", name="确认连接这一页", exact=True)).to_be_enabled()
                await page.get_by_role("button", name="确认连接这一页", exact=True).click()
                await expect(page.get_by_role("button", name="投递（辅助填写）", exact=True)).to_be_enabled()
                assert [path.rsplit("/", 1)[-1] for _, path, _ in writes] == ["preview-existing", "confirm-existing"]
        if connected or fresh_bind:
            # Existing snapshots may restore directly into the workspace or via
            # the local setup confirmation; neither path can perform a write.
            await expect(page.get_by_role("button", name="投递工作台", exact=True)).to_be_enabled()
            await page.wait_for_function("() => document.querySelector('.application-session') || document.querySelector('.application-setup select:not(:disabled)')")
            await enter_workspace()
            if connected:
                assert f"/api/browser/{SID}/snapshot" in reads and f"/api/browser/{SID}/workflow" in reads
                assert not writes, "Restoring an occupied form must only read"
            primary = page.get_by_role("button", name="投递（辅助填写）", exact=True)
            await expect(primary).to_be_enabled()
            operation_text = await page.locator('[data-workspace-section="operation"]').inner_text()
            for forbidden in ("允许本轮自动填写本人身份证号码", "选择招聘机构", "岗位分析", "先在招聘网站登录"):
                assert forbidden not in operation_text, forbidden
            assert await page.get_by_role("button", name=AUDIT_BUTTON, exact=True).count() == 0
            await primary.click()
            await expect(page.get_by_role("button", name="继续辅助填写", exact=True)).to_be_enabled()
            expected_prefix = ["preview-existing", "confirm-existing", "start"] if fresh_bind else []
            assert [path.rsplit("/", 1)[-1] for _, path, _ in writes] == [*expected_prefix, "assist"], writes
            assert len([path for _, path, _ in writes if path.endswith("/assist")]) == 1

            await page.get_by_role("button", name="待补充资料", exact=False).click()
            await assert_sections("questions")
            questions = page.locator('[data-workspace-section="questions"]')
            await expect(questions.locator(".answer-row")).to_have_count(3 if checkboxes else 1)
            answer = questions.get_by_role("combobox", name=QUESTION, exact=True)
            await expect(answer).to_be_visible()
            assert await answer.locator("option").all_text_contents() == ["请选择真实答案", "是", "否"]
            question_text = await questions.inner_text()
            assert "未识别字段1" not in question_text and "你的可到岗城市是？" not in question_text
            await answer.select_option("#mobility-no")
            await page.get_by_role("button", name="投递操作", exact=True).click()
            await page.get_by_role("button", name="待补充资料", exact=False).click()
            await expect(answer).to_have_value("#mobility-no")
            assert len(writes) == len(expected_prefix) + 1, "Switching sections must preserve drafts without sending them"
            await answer.select_option("")
            if checkboxes:
                source_group = questions.get_by_role("group", name=SOURCE_QUESTION, exact=True)
                technology_group = questions.get_by_role("group", name=TECHNOLOGY_QUESTION, exact=True)
                await expect(source_group).to_be_visible()
                await expect(technology_group).to_be_visible()
                await expect(source_group.get_by_role("checkbox")).to_have_count(len(SOURCE_OPTIONS))
                await expect(technology_group.get_by_role("checkbox")).to_have_count(len(TECHNOLOGY_OPTIONS))
                assert await source_group.locator("label").all_text_contents() == SOURCE_OPTIONS
                assert await technology_group.locator("label").all_text_contents() == TECHNOLOGY_OPTIONS
                # Only the separate radio question uses a select; neither
                # checkbox group creates per-option yes/no menus.
                await expect(questions.get_by_role("combobox")).to_have_count(1)
                await expect(source_group.get_by_role("checkbox", name=SOURCE_OPTIONS[2], exact=True)).to_be_checked()
                await expect(technology_group.get_by_role("checkbox", name=TECHNOLOGY_OPTIONS[1], exact=True)).to_be_checked()
                await source_group.get_by_role("checkbox", name=SOURCE_OPTIONS[0], exact=True).check()
                await source_group.get_by_role("checkbox", name=SOURCE_OPTIONS[2], exact=True).uncheck()
                await page.get_by_role("button", name="投递操作", exact=True).click()
                await assert_sections("operation")
                await page.get_by_role("button", name="开发检查", exact=True).click()
                await assert_sections("diagnostics")
                await page.get_by_role("button", name="待补充资料", exact=False).click()
                await assert_sections("questions")
                await expect(source_group.get_by_role("checkbox", name=SOURCE_OPTIONS[0], exact=True)).to_be_checked()
                await expect(source_group.get_by_role("checkbox", name=SOURCE_OPTIONS[2], exact=True)).not_to_be_checked()
                await expect(technology_group.get_by_role("checkbox", name=TECHNOLOGY_OPTIONS[1], exact=True)).to_be_checked()
                assert len(writes) == 1, "Editing a checklist or switching branches must not write or mix the independent group's state"
                await questions.get_by_role("button", name="保存答案并继续填写", exact=True).click()
                await expect(page.locator(".notice")).to_contain_text("匿名测试已截获复选题填写计划；没有执行网页或保存记忆。")
                await expect(source_group.get_by_role("checkbox", name=SOURCE_OPTIONS[0], exact=True)).to_be_checked()
                await expect(source_group.get_by_role("checkbox", name=SOURCE_OPTIONS[2], exact=True)).not_to_be_checked()
                assert [path.rsplit("/", 1)[-1] for _, path, _ in writes] == ["assist", "execute"], writes
                assert not errors and not unexpected, {"errors": errors, "unexpected": unexpected}
                return
            await page.get_by_role("button", name="开发检查", exact=True).click()
            await assert_sections("diagnostics")
            audit = page.get_by_role("button", name=AUDIT_BUTTON, exact=True)
            await expect(audit).to_be_enabled()
            diagnostics = page.locator('[data-workspace-section="diagnostics"]')
            await expect(diagnostics).to_contain_text("尚未读取完整题干")
            await expect(diagnostics).to_contain_text("网页真实选项尚未完整读取")
            await audit.click()
            await expect(diagnostics).to_contain_text("模型：fixture-model")
            assert state["audit_count"] == 1
            assert [path.rsplit("/", 1)[-1] for _, path, _ in writes] == [*expected_prefix, "assist", "extraction-audit"], writes
            await page.get_by_role("button", name="投递操作", exact=True).click()
            assert await page.get_by_role("button", name=AUDIT_BUTTON, exact=True).count() == 0
            await page.get_by_role("button", name="开发检查", exact=True).click()
            await expect(diagnostics).to_contain_text("模型：fixture-model")
            assert state["audit_count"] == 1, "Section switching must preserve the audit result"
            await page.get_by_role("button", name="投递操作", exact=True).click()
            write_count = len(writes)
            await page.get_by_role("button", name="同步当前页", exact=True).click()
            await expect(page.get_by_role("button", name="投递（辅助填写）", exact=True)).to_be_enabled()
            assert len(writes) == write_count, "Explicit synchronization must not replay filling or audit"
            await page.reload()
            await page.get_by_role("button", name="投递工作台", exact=True).click()
            await enter_workspace()
            await expect(page.get_by_role("button", name="投递（辅助填写）", exact=True)).to_be_enabled()
            assert len(writes) == write_count, "Reloading must not replay the previous assist"

        if mobile:
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile workspace overflowed the viewport"
        assert not errors and not unexpected, {"errors": errors, "unexpected": unexpected}
    finally:
        await context.close()


async def main():
    dist = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "frontend" / "dist"
    assert (dist / "index.html").is_file(), f"Build the frontend first: {dist}"
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            await case(browser, dist, connected=False)
            await case(browser, dist, connected=True)
            await case(browser, dist, connected=False, fresh_bind=True)
            await case(browser, dist, connected=False, mobile=True)
            await case(browser, dist, connected=True, checkboxes=True)
        finally:
            await browser.close()
    print("simpleApplicationFlow.ui_test: OK (anonymous setup, single assist, question evidence, checkbox groups/drafts/explicit true-false payload, hidden panels, read-only audit/recovery, mobile)")


if __name__ == "__main__":
    asyncio.run(main())
