"""Built React UI with intercepted anonymous APIs only; never use real accounts.

An occupied homepage/job-list session can be restored for observation. It must
not become permission to select an organization, navigate jobs or start filling.
"""
import asyncio
import mimetypes
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from app.application_models import ApplicationWorkflowState
from app.browser_models import BrowserSnapshot, NavigationCandidate
from app.models import CandidateProfile, ResumeProfile, ResumeRecord
from playwright.async_api import async_playwright, expect

ORIGIN = "https://zhida-ui.invalid"
URL = "https://ats-fixture.example.test/campus"
SID = "organization-ui"
RID = "anonymous-cv"
WRONG_PAGE = "还没有确认这是信息填写页。请先在官网进入填写页面，然后重新同步；不会自动登录或换岗位。"


async def case(browser, dist, stage):
    now = datetime.now(timezone.utc)
    profile = CandidateProfile(name="匿名测试")
    resume = ResumeRecord(id=RID, filename="anonymous.pdf", label="匿名 Agent 简历", parser="fixture",
                          profile=ResumeProfile(), is_default=True, status="completed",
                          created_at=now, updated_at=now)
    snapshot = BrowserSnapshot(session_id=SID, url=URL, title="合成招聘首页" if stage == "homepage" else "合成岗位列表",
                               browser_engine="safari", fields=[])
    candidates = [NavigationCandidate(id=key, label=label, kind="browse_jobs", entry_scope="organization",
                                      requires_user_choice=True)
                  for key, label in (("head", "总行"), ("branch", "分行"), ("sub", "子公司"))]
    candidates.append(NavigationCandidate(id="jobs", label="招聘职位", kind="browse_jobs", url=URL + "/jobs"))
    workflow = ApplicationWorkflowState(session_id=SID, url=URL, title=snapshot.title, stage=stage,
                                         navigation_candidates=candidates)
    reads, writes, unexpected, errors = [], [], [], []
    headers = {"access-control-allow-origin": ORIGIN, "access-control-allow-credentials": "true",
               "access-control-allow-methods": "GET,OPTIONS", "access-control-allow-headers": "content-type"}

    async def intercept(route):
        request = route.request
        parsed = urlparse(request.url)
        path = parsed.path
        if parsed.hostname in {"fonts.googleapis.com", "fonts.gstatic.com"} and request.method == "GET":
            await route.fulfill(body="", content_type="text/css")
            return
        if parsed.hostname == "zhida-ui.invalid" and parsed.port in {None, 8000} and path.startswith("/api/"):
            if request.method == "OPTIONS":
                await route.fulfill(status=204, headers=headers)
                return
            if request.method != "GET":
                writes.append((request.method, path, request.post_data))
                await route.abort()
                return
            reads.append(path)
            body = None
            if path == "/api/auth/me":
                body = {"id": "anonymous", "email": "fixture@example.invalid", "display_name": "匿名测试",
                        "created_at": now.isoformat(), "is_local": True}
            elif path == "/api/profile": body = profile.model_dump(mode="json")
            elif path == "/api/resumes": body = [resume.model_dump(mode="json")]
            elif path in {"/api/conflicts", "/api/application-knowledge", "/api/application-knowledge/targets"}: body = []
            elif path == "/api/browser/current":
                body = {"session_id": SID, "resume_id": RID, "occupied": True, "assistance_version": 1,
                        "record_completion_version": 1, "journey_version": 1}
            elif path == f"/api/browser/{SID}/snapshot": body = snapshot.model_dump(mode="json")
            elif path == f"/api/browser/{SID}/workflow": body = workflow.model_dump(mode="json")
            elif path == f"/api/browser/{SID}/observation-consent": body = {"enabled": False, "context_token": "fixture-read-only"}
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

    context = await browser.new_context(service_workers="block", viewport={"width": 1280, "height": 900})
    page = await context.new_page()
    page.set_default_timeout(5000)
    page.on("pageerror", lambda error: errors.append(str(error)))

    async def block_socket(socket):
        unexpected.append("WebSocket")
        await socket.close()

    await page.route_web_socket("**/*", block_socket)
    await page.route("**/*", intercept)
    try:
        await page.goto(ORIGIN + "/")
        await page.get_by_role("button", name="投递工作台", exact=True).click()
        setup = page.locator(".application-setup")
        await expect(setup.get_by_label("投递简历", exact=True)).to_have_value(RID)
        await expect(setup.get_by_label("信息填写页网址", exact=True)).to_have_value(URL)
        assert f"/api/browser/{SID}/snapshot" in reads and f"/api/browser/{SID}/workflow" in reads
        assert not writes, "Restoring a homepage/job list must only observe the saved session"
        await setup.get_by_role("button", name="确认并继续", exact=True).click()
        workspace = page.get_by_role("region", name="本次投递工作台", exact=True)
        operation = workspace.locator('[data-workspace-section="operation"]')
        await expect(operation.get_by_text(WRONG_PAGE, exact=True)).to_be_visible()
        primary = operation.get_by_role("button", name="投递（辅助填写）", exact=True)
        await expect(primary).to_be_disabled()
        assert not writes, "Setup confirmation cannot navigate a non-form page or start filling"
        for forbidden in ("选择招聘机构", "总行", "分行", "子公司", "招聘职位", "让职达继续", "进入申请页面"):
            assert await workspace.get_by_role("button", name=forbidden, exact=True).count() == 0, forbidden
        assert "先选择要申请的招聘机构" not in await operation.inner_text()
        assert "岗位分析" not in await operation.inner_text()
        count = len(reads)
        await operation.get_by_role("button", name="同步当前页", exact=True).click()
        await expect(primary).to_be_disabled()
        await expect(operation.get_by_text(WRONG_PAGE, exact=True)).to_be_visible()
        assert reads[count:].count("/api/browser/current") == 1
        assert f"/api/browser/{SID}/snapshot" in reads[count:] and f"/api/browser/{SID}/workflow" in reads[count:]
        assert not writes, "Syncing can observe a homepage/job list but must not select candidates or replay actions"
        await workspace.get_by_role("button", name="开发检查", exact=True).click()
        diagnostics = workspace.locator('[data-workspace-section="diagnostics"]')
        await expect(diagnostics).to_be_visible()
        expected_stage = "招聘首页" if stage == "homepage" else "岗位列表"
        await expect(diagnostics.get_by_text(f"页面：{snapshot.title} · 0 个控件 · {expected_stage}", exact=True)).to_be_visible()
        await expect(diagnostics.get_by_role("button", name="仅分析，不填写", exact=True)).to_be_disabled()
        await workspace.get_by_role("button", name="投递操作", exact=True).click()
        await expect(primary).to_be_disabled()
        assert not writes and not unexpected and not errors, (stage, writes, unexpected, errors)
        assert not any(path.startswith("/api/jobs/") or "/assist" in path or "/agent/" in path for path in reads), reads
    finally:
        await context.close()


async def main():
    dist = (ROOT / "frontend" / "dist").resolve()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        try:
            for stage in ("homepage", "job_list"):
                await case(browser, dist, stage)
        finally:
            await browser.close()
    print("workbench_organization_ui_test: OK (homepage/job-list read-only restore, form-page required, no organization/job navigation or writes)")


if __name__ == "__main__":
    asyncio.run(main())
