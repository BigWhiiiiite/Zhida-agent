"""Offline window-focus, exact-tab and pre-DOM URL regression; no real Safari."""
import asyncio
import json
from unittest.mock import patch

from app.safari_browser import CREATE_WINDOW_WITH_URL, SafariContext
from app.safari_window_registry import SafariWindowRegistry


class FocusFixture:
    def __init__(self):
        self.calls = []
        self.creates = 0
        self.front = 101
        self.tabs = {(101,1):"http://127.0.0.1:5173/"}
        self.expected = "https://jobs.example.test/resumeEdit?postId=135601"
        self.changed = False

    async def __call__(self, script, *args):
        self.calls.append((script,args))
        if script == CREATE_WINDOW_WITH_URL:
            self.creates += 1
            # Model Safari focus moving back to the frontend after activation.
            # The verified ID must already have been captured, not queried now.
            assert script.index("set ownedWindowId") < script.rindex("activate")
            assert script.index('make new document') < script.index('activate') < script.index('repeat 30 times')
            assert script.index("is bindingUrl") < script.index("set URL")
            assert 'repeat 30 times' in script and 'set ownedWindowId to candidateId' in script
            assert 'if ownedWindowId is 0 then error' in script
            assert 'set ownedTabIndex to index of current tab' in script
            assert 'set URL of tab ownedTabIndex' in script
            assert "return id of front window" not in script
            self.tabs[(271,1)] = args[0]
            self.tabs[(271,2)] = "http://127.0.0.1:5173/"
            self.front = 101
            return "271|1|" + args[1]
        assert args == ("271","1"), "Never inspect private window or use the selected second tab"
        assert "every window" not in script and "every tab" not in script
        if "do JavaScript" in script:
            assert "in tab tabIndex of window id windowId" in script
            assert "in current tab" not in script
            assert "if (location.href !==" in script, "Stop page changes before running a DOM expression"
            if self.changed:
                return json.dumps({"ok":False,"url":"http://127.0.0.1:5173/","title":"","code":"ZHIDA_PAGE_CHANGED"})
            return json.dumps({"ok":True,"url":self.tabs[(271,1)],"title":"合成招聘表","value":True})
        if "return URL of tab tabIndex" in script:
            return self.tabs[(271,1)]
        return "271"


async def run():
    runner = FocusFixture()
    registry = SafariWindowRegistry(lambda:SafariContext(runner))
    entry,_ = await registry.open("fixture",runner.expected)
    page = entry.page
    assert (page.window_id,page.tab_index) == (271,1)
    # A local metadata validator keeps this regression independent of DNS.
    def validate(url):
        if not url.startswith("https://jobs.example.test/"):
            raise ValueError("Unsafe test URL")
    page.url_validator = validate
    assert await page.evaluate("() => document.title") is True
    assert runner.front == 101 and page.url == runner.expected
    js_count = sum("do JavaScript" in script for script,_ in runner.calls)
    runner.tabs[(271,1)] = "http://127.0.0.1:5173/"
    try:
        await page.evaluate("() => document.body.innerText")
        raise AssertionError("Read the local frontend DOM")
    except ValueError as exc:
        assert "Unsafe" in str(exc)
    assert sum("do JavaScript" in script for script,_ in runner.calls) == js_count
    runner.tabs[(271,1)] = runner.expected
    runner.changed = True
    try:
        await page.evaluate("() => document.querySelector('input').value='must not write'")
        raise AssertionError("A redirect between metadata check and operation was accepted")
    except ValueError as exc:
        assert "地址检查后" in str(exc)
    assert page._title == ""

    # Missing/forged proof must not create a page handle or inspect/close anyone.
    for response in ("101", "101|1|about:blank#wrong", "271|2|matching-token", "0|1|bad", "271|0|bad"):
        async def forged(script,*args):
            assert script == CREATE_WINDOW_WITH_URL
            return response
        context = SafariContext(forged)
        try:
            await context.new_page(runner.expected)
            raise AssertionError("Unverified frontend window was adopted")
        except RuntimeError as exc:
            assert "可验证" in str(exc)
        assert not context.pages
    async def pinned_tabs(script,*args):
        # The newly created active tab need not be index 1 (e.g. pinned tabs).
        # Its unique nonce, not its ordinal, is the ownership proof.
        return '271|6|' + args[1]
    context=SafariContext(pinned_tabs)
    page=await context.new_page(runner.expected)
    assert page.tab_index==6 and page.window_id==271
    print("safari_binding_test: OK (focus-independent verified creation, pinned tab, pre-DOM URL checks, redirect guard, forged binding rejected)")


if __name__ == "__main__":
    asyncio.run(run())
