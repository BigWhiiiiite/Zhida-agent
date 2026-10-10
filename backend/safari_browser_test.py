"""Offline Safari contract tests; never launch/inspect a real user browser."""
import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

from app.browser_loading import wait_for_rendered_content
from app.browser_models import BrowserSnapshot
from app.browser_service import BrowserDemoService, configured_browser_engine
from app.safari_browser import CREATE_WINDOW, CREATE_WINDOW_WITH_URL, SafariContext, SafariHandle, apple_literal


class FakeAppleEvents:
    def __init__(self):
        self.calls = []
        self.value = True
        self.error = ""
        self.url = "https://recruiting.example.test/campus"

    async def __call__(self, script, *args):
        self.calls.append((script, args))
        if script == CREATE_WINDOW:
            return "271|1|" + args[0]
        if script == CREATE_WINDOW_WITH_URL:
            assert len(args) == 2 and args[0].startswith("https://")
            self.url = args[0]
            return "271|1|" + args[1]
        assert args == ("271","1"), "Never connect another Safari window/tab"
        if "close window id" in script:
            return ""
        if "return URL of tab tabIndex" in script:
            return self.url
        if self.error:
            raise RuntimeError(self.error)
        if "set URL" in script:
            return ""
        if "document.readyState" in script:
            value = "complete"
        else:
            value = self.value
        return json.dumps({"ok": True, "url": self.url, "title": "合成招聘页", "value": value})


async def run():
    for platform, expected in (("darwin", "safari"), ("linux", "chromium")):
        with patch.dict(os.environ, {"APP_BROWSER_ENGINE": "auto"}), patch("app.browser_service.sys.platform", platform):
            assert configured_browser_engine() == expected
    with patch.dict(os.environ, {"APP_BROWSER_ENGINE": "chromium"}):
        assert configured_browser_engine() == "chromium"
    with patch.dict(os.environ, {"APP_BROWSER_ENGINE": "unknown"}):
        try:
            configured_browser_engine()
            raise AssertionError("Unknown engine accepted")
        except ValueError:
            pass

    runner = FakeAppleEvents()
    context = SafariContext(runner)
    page = await context.new_page()
    assert page.window_id == 271
    assert page.url == runner.url
    assert await context.cookies([runner.url]) == [], "Do not fabricate cookie/login evidence"
    await page.goto(runner.url)
    locator = page.locator('input[name="candidate"]')
    await locator.fill('匿名答案\n"\\with escaping')
    await page.keyboard.press("Escape")
    assert all("every window" not in script and "every tab" not in script for script, _ in runner.calls)
    assert any("HTMLInputElement.prototype" in script for script, _ in runner.calls)
    assert "\\\"" in apple_literal('"') and "\\n" in apple_literal('\n')
    try:
        await page.keyboard.press("Enter")
        raise AssertionError("Implicit form submission allowed")
    except ValueError:
        pass
    count = len(runner.calls)
    for operation in (lambda: locator.set_input_files("private-resume.pdf"), lambda: page.expect_popup()):
        try:
            value = operation()
            if asyncio.iscoroutine(value):
                await value
            raise AssertionError("Unsupported operation claimed success")
        except ValueError:
            pass
    assert len(runner.calls) == count
    other = SafariContext(FakeAppleEvents())
    other_page = await other.new_page()
    try:
        page._argument(SafariHandle(other_page, "foreign"))
        raise AssertionError("Cross-window handle accepted")
    except ValueError:
        pass
    await context.close()
    assert page.is_closed()
    assert "close window id windowId" in runner.calls[-1][0]
    assert "quit" not in runner.calls[-1][0]

    runner = FakeAppleEvents()
    runner.error = "Safari JavaScript 权限未启用"
    try:
        await SafariContext(runner).new_page()
        raise AssertionError("Permission failure accepted")
    except RuntimeError:
        pass
    assert "close window id" in runner.calls[-1][0]
    assert not any("set URL" in script for script, _ in runner.calls)

    # Ordinary navigation comes before any JavaScript permission check.
    runner = FakeAppleEvents()
    runner.error = "Safari JavaScript 权限未启用"
    context = SafariContext(runner)
    page = await context.new_page("https://recruiting.example.test/campus?job=agent#apply")
    assert len(runner.calls) == 1
    assert runner.calls[0][0] == CREATE_WINDOW_WITH_URL
    assert runner.calls[0][1][0] == "https://recruiting.example.test/campus?job=agent#apply"
    assert runner.calls[0][1][1].startswith("about:blank#zhida-")
    assert page.url.endswith("?job=agent#apply")
    context.release_windows()
    await context.close()
    assert len(runner.calls) == 1, "Releasing a failed connection must not close the site"
    assert page.is_closed() and not context.pages, "Stale automation handles must be revoked"

    page = AsyncMock()
    page.evaluate.side_effect = [False, True, True]
    with patch("app.browser_loading.asyncio.sleep", AsyncMock()):
        await wait_for_rendered_content(page)
    assert page.evaluate.await_count == 3
    page.evaluate = AsyncMock(return_value=False)
    try:
        await wait_for_rendered_content(page, timeout_seconds=0)
        raise AssertionError("Blank page treated as ready")
    except RuntimeError as exc:
        assert "停止分析和填写" in str(exc)

    # Native routing does not start Playwright or silently fall back to Chrome.
    native_runner = FakeAppleEvents()
    context = SafariContext(native_runner)
    snapshot = BrowserSnapshot(session_id="fixture", url=runner.url, title="合成招聘页", fields=[], browser_engine="safari")
    service = BrowserDemoService()
    service._validate_url = lambda url: url
    service.snapshot = AsyncMock(return_value=snapshot)
    with patch.dict(os.environ, {"APP_BROWSER_ENGINE": "safari", "APP_BROWSER_HEADLESS": "false"}), \
            patch("app.browser_service.sys.platform", "darwin"), \
            patch("app.browser_service.SafariContext", lambda: context), \
            patch("app.browser_service.wait_for_rendered_content", AsyncMock()), \
            patch("app.browser_service.async_playwright") as playwright:
        result = await service.start("https://recruiting.example.test/campus")
        assert result.browser_engine == "safari"
        playwright.assert_not_called()
        assert service.playwright is None and service.browser is None
        assert native_runner.calls[0][0] == CREATE_WINDOW_WITH_URL
        assert not any("set URL" in script for script, _ in native_runner.calls[1:]), "Don't reload the newly opened site after its verified creation"
        await service.close()

    # No DOM analysis if the new native window remains empty.
    service = BrowserDemoService()
    service._validate_url = lambda url: url
    service.snapshot = AsyncMock()
    native_runner = FakeAppleEvents()
    context = SafariContext(native_runner)
    with patch.dict(os.environ, {"APP_BROWSER_ENGINE": "safari", "APP_BROWSER_HEADLESS": "false"}), \
            patch("app.browser_service.sys.platform", "darwin"), \
            patch("app.browser_service.SafariContext", lambda: context), \
            patch("app.browser_service.wait_for_rendered_content", AsyncMock(side_effect=RuntimeError("blank"))):
        try:
            await service.start("https://recruiting.example.test/campus")
            raise AssertionError("Blank page accepted")
        except RuntimeError as exc:
            assert "窗口已保留" in str(exc)
        service.snapshot.assert_not_called()
        assert service.session_id is None and service.context is None
        assert not any("close window id" in script for script, _ in native_runner.calls)

    # Missing permission invalidates automation, not ordinary navigation.
    native_runner = FakeAppleEvents()
    native_runner.error = "Safari JavaScript 权限未启用"
    context = SafariContext(native_runner)
    service = BrowserDemoService()
    service._validate_url = lambda url: url
    service.snapshot = AsyncMock()
    with patch.dict(os.environ, {"APP_BROWSER_ENGINE": "safari", "APP_BROWSER_HEADLESS": "false"}), \
            patch("app.browser_service.sys.platform", "darwin"), \
            patch("app.browser_service.SafariContext", lambda: context), \
            patch("app.browser_service.async_playwright") as playwright:
        try:
            await service.start("https://recruiting.example.test/campus")
            raise AssertionError("Missing JavaScript permission accepted")
        except RuntimeError as exc:
            assert "窗口已保留" in str(exc) and "没有执行填写" in str(exc)
        assert native_runner.calls[0][0] == CREATE_WINDOW_WITH_URL
        assert not any("close window id" in script for script, _ in native_runner.calls)
        assert service.session_id is None and service.context is None and not context.pages
        service.snapshot.assert_not_called()
        playwright.assert_not_called()
    print("safari_browser_test: OK (native route, owned window, permission/blank gates, explicit limitations)")


if __name__ == "__main__":
    asyncio.run(run())
