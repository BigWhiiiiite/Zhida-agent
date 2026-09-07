"""Headless end-to-end test for semantic discovery, upload, fill and verification."""
from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.async_api import async_playwright

from app.browser_models import ExecutePlanRequest, FillAction
from app.browser_service import BrowserDemoService


HTML = """
<!doctype html><html><body><form>
  <label>Full name ✱<input name="full_name" required></label>
  <div class="application-question">
    <div class="application-label">Why do you want this role? ✱</div>
    <div class="application-field"><textarea name="cards[test][field0]" required></textarea></div>
  </div>
  <label>Location ✱<select name="location" required>
    <option value="">Select...</option><option value="CN">China</option>
  </select></label>
  <label>Resume/CV ✱<input type="file" name="resume" required style="display:none"></label>
  <button type="button">Submit application</button>
</form></body></html>
"""


async def main() -> None:
    with TemporaryDirectory() as temporary:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content(HTML)

            service = BrowserDemoService()
            service.page = page
            service.session_id = "browser-smoke"
            snapshot = await service.snapshot()
            by_name = {field.name: field for field in snapshot.fields}
            assert by_name["cards[test][field0]"].label.startswith("Why do you want this role?")
            assert by_name["resume"].field_type == "file"

            resume = Path(temporary) / "resume.txt"
            resume.write_text("Test Candidate", encoding="utf-8")
            actions = [
                FillAction(selector=by_name["full_name"].selector, label="Full name", action="fill",
                           value="Test Candidate", confidence=1),
                FillAction(selector=by_name["cards[test][field0]"].selector, label="Why this role", action="fill",
                           value="I enjoy building reliable agents.", confidence=1, user_confirmed=True),
                FillAction(selector=by_name["location"].selector, label="Location", action="select",
                           value="China", confidence=1),
            ]
            result = await service.execute("browser-smoke", ExecutePlanRequest(actions=actions), resume)
            assert result.failed == 0
            assert result.verified == 4
            assert result.pre_submit.ready
            assert result.pre_submit.human_challenges == []
            assert result.pre_submit.file_uploads == ["resume.txt"]
            assert result.pre_submit.submit_labels == ["Submit application"]
            await page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div class=\"h-captcha\"></div>')")
            challenge_check = await service.pre_submit_check("browser-smoke")
            assert not challenge_check.ready
            assert challenge_check.human_challenges == ["页面包含需要用户完成的人机验证"]
            await context.close()
            await browser.close()


asyncio.run(main())
print("Zhida browser smoke test passed")
