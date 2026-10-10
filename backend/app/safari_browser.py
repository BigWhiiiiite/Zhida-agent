"""Local native-Safari transport for Zhida's existing form executor.

No WebDriver port, browser extension, global preference writes, or personal
tab enumeration. Only the new window created for this task is addressed.
Apple Events/JavaScript permission must be granted by the user in Safari.
This is a deliberately bounded Playwright-shaped adapter, not a claim to
implement all of Playwright. Unsupported operations fail explicitly.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from typing import Any
from uuid import uuid4


SAFARI_SETUP = (
    "请在 Safari → 设置 → 高级，开启‘显示网页开发者功能’，再在开发者设置中"
    "开启‘允许来自 Apple 事件的 JavaScript’（Allow JavaScript from Apple Events）。"
    "如 macOS 询问是否允许运行职达的终端/Python 控制 Safari，请自行确认。"
    "不需要 Chrome 扩展，也不需要开启完整 CDP 或 WebDriver 远程自动化。"
)

# Safari's dictionary exposes a window ID, but no persistent tab ID. Prove
# ownership using an unpredictable blank URL *before* navigation/activation,
# then pin the single newly-created tab. Never adopt a current/front window
# simply because it exists, or search personal windows for a matching URL.
CREATE_WINDOW = '''on run argv
  set bindingUrl to item 1 of argv
  tell application "Safari"
    make new document with properties {URL:bindingUrl}
    activate
    set ownedWindowId to 0
    set ownedTabIndex to 0
    repeat 30 times
      try
        set candidateId to id of front window
        if (URL of current tab of window id candidateId) is bindingUrl then
            set ownedWindowId to candidateId
            set ownedTabIndex to index of current tab of window id candidateId
            exit repeat
        end if
      end try
      delay 0.1
    end repeat
    if ownedWindowId is 0 then error "ZHIDA_WINDOW_BINDING_FAILED"
    activate
    return (ownedWindowId as text) & "|" & (ownedTabIndex as text) & "|" & bindingUrl
  end tell
end run'''
CREATE_WINDOW_WITH_URL = '''on run argv
  set targetUrl to item 1 of argv
  set bindingUrl to item 2 of argv
  tell application "Safari"
    make new document with properties {URL:bindingUrl}
    activate
    set ownedWindowId to 0
    set ownedTabIndex to 0
    repeat 30 times
      try
        set candidateId to id of front window
        if (URL of current tab of window id candidateId) is bindingUrl then
            set ownedWindowId to candidateId
            set ownedTabIndex to index of current tab of window id candidateId
            exit repeat
        end if
      end try
      delay 0.1
    end repeat
    if ownedWindowId is 0 then error "ZHIDA_WINDOW_BINDING_FAILED"
    if (URL of tab ownedTabIndex of window id ownedWindowId) is not bindingUrl then error "ZHIDA_WINDOW_BINDING_FAILED"
    set URL of tab ownedTabIndex of window id ownedWindowId to targetUrl
    activate
    return (ownedWindowId as text) & "|" & (ownedTabIndex as text) & "|" & bindingUrl
  end tell
end run'''
WINDOW_COMMAND = '''on run argv
  set windowId to item 1 of argv as integer
  set tabIndex to item 2 of argv as integer
  tell application "Safari"
    if not (exists window id windowId) then error "ZHIDA_WINDOW_CLOSED"
    if not (exists tab tabIndex of window id windowId) then error "ZHIDA_TAB_CLOSED"
    __COMMAND__
  end tell
end run'''

# Only the user-selected front tab, and only when its address exactly matches
# the destination they supplied. No enumeration, title/DOM read, or navigation.
CAPTURE_EXISTING_WINDOW = '''on run argv
  set expectedUrl to item 1 of argv
  tell application "Safari"
    if (count of windows) is 0 then error "ZHIDA_WINDOW_CLOSED"
    set selectedWindowId to id of front window
    set selectedTabIndex to index of current tab of window id selectedWindowId
    set selectedUrl to URL of current tab of window id selectedWindowId
    if selectedUrl is not expectedUrl then error "ZHIDA_ATTACHMENT_URL_MISMATCH"
    return (selectedWindowId as text) & "|" & (selectedTabIndex as text) & "|" & selectedUrl
  end tell
end run'''


def apple_literal(value: str) -> str:
    """A string literal, not executable interpolation or a shell command."""
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\r', '\\r').replace('\n', '\\n') + '"'


async def run_applescript(script: str, *args: str) -> str:
    if sys.platform != "darwin":
        raise RuntimeError("Safari 原生填写仅支持 macOS；其他系统请设置 APP_BROWSER_ENGINE=chromium")
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/osascript", "-", *args,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(script.encode("utf-8")), 20)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        # Stop only this owned subprocess, not Safari or another user's task.
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise RuntimeError("Safari 连接等待超时，请检查是否有系统授权对话框，再重试。") from None
    if process.returncode:
        error = stderr.decode("utf-8", errors="replace")
        if "ZHIDA_WINDOW_BINDING_FAILED" in error:
            raise RuntimeError("Safari 新建页归属校验失败，未连接最前面的其他窗口。请重新点击‘用 Safari 打开招聘网站’，不要在打开过程中切换窗口。")
        if "ZHIDA_WINDOW_CLOSED" in error or "ZHIDA_TAB_CLOSED" in error:
            raise LookupError("职达的 Safari 招聘窗口已关闭，请重新打开任务")
        if "ZHIDA_ATTACHMENT_URL_MISMATCH" in error:
            raise ValueError("置前的 Safari 标签页与填写链接不一致，未连接任何页面。请把目标招聘填写页置前，并核对工作台中的完整网址。")
        if "ZHIDA_BOUND_TAB_CHANGED" in error:
            raise ValueError("已连接窗口的选中标签页发生变化，已停止。请切回原招聘填写标签页后重新识别。")
        # Never echo AppleScript, JavaScript, form values or credentials.
        if "-1743" in error or "not authorized" in error.lower():
            raise RuntimeError("macOS 尚未允许职达控制 Safari。" + SAFARI_SETUP)
        if "javascript" in error.lower() or "JavaScript" in error:
            raise RuntimeError("Safari 尚未允许职达识别网页。" + SAFARI_SETUP)
        raise RuntimeError("Safari 操作未完成。请检查招聘窗口及系统授权提示。" + SAFARI_SETUP)
    return stdout.decode("utf-8").strip()


DOM_HELPERS = r'''
const visible = el => !!el && !el.closest('[hidden],[aria-hidden="true"]') &&
  el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden';
const enabled = el => !!el && !el.disabled && !el.closest('[aria-disabled="true"]');
const query = (root, selector) => {
  if (/:(?:has-text|text|nth-match)\(/.test(selector) || selector.includes('>>'))
    throw new Error('ZHIDA_UNSUPPORTED_SELECTOR');
  const needsVisible = /:visible\b/.test(selector);
  const css = selector.replace(/:visible\b/g, '');
  return [...root.querySelectorAll(css)].filter(el => !needsVisible || visible(el));
};
const one = nodes => {
  if (nodes.length !== 1) throw new Error('ZHIDA_AMBIGUOUS_CONTROL');
  return nodes[0];
};
const writable = el => {
  if (!enabled(el) || el.readOnly || !visible(el)) throw new Error('ZHIDA_CONTROL_NOT_EDITABLE');
};
'''


class SafariContext:
    def __init__(self, runner=run_applescript):
        self.runner = runner
        self.pages: list[SafariPage] = []
        self.lock = asyncio.Lock()

    async def new_page(self, url: str | None = None) -> "SafariPage":
        binding_url = "about:blank#zhida-" + uuid4().hex
        async with self.lock:
            result = (await self.runner(CREATE_WINDOW_WITH_URL, url, binding_url)
                      if url else await self.runner(CREATE_WINDOW, binding_url))
        parts = result.split("|")
        if (len(parts) != 3 or not re.fullmatch(r"[1-9]\d*", parts[0]) or
                not re.fullmatch(r"[1-9]\d*", parts[1]) or parts[2] != binding_url):
            raise RuntimeError("Safari 未返回可验证的新建招聘页编号，未连接其他窗口；请重新用职达打开招聘网站")
        page = SafariPage(self, int(parts[0]), int(parts[1]))
        self.pages.append(page)
        if url:
            # Open the actual site first. DOM/JavaScript permission is only
            # needed for connection, never for ordinary website navigation.
            page.url = url
        else:
            # Empty-document checks may clean up their own blank window.
            try:
                await page.evaluate("() => true")
            except Exception:
                await page.close()
                raise
        return page

    def release_windows(self) -> None:
        """Leave created windows open for the user, revoke automation handles."""
        for page in self.pages:
            page._closed = True
        self.pages.clear()

    async def cookies(self, urls=None) -> list[dict]:
        # Authentication detection uses DOM and storage evidence too. Safari
        # cannot enumerate HttpOnly cookies via this transport; do not scrape
        # browser databases or return fake login evidence.
        return []

    async def close(self) -> None:
        for page in self.pages:
            await page.close()


class SafariPage:
    def __init__(self, context: SafariContext, window_id: int, tab_index: int = 1):
        self.context = context
        self.window_id = window_id
        self.tab_index = tab_index
        self.url_validator = None
        self.url = "about:blank"
        self._title = ""
        self._closed = False
        self._preserve_window = False
        self._require_selected_tab = False
        self._registry = "__zhidaHandles_" + uuid4().hex
        self.keyboard = SafariKeyboard(self)

    def is_closed(self) -> bool:
        return self._closed

    async def ensure_available(self) -> None:
        # Check only the recorded window ID. No JavaScript, URL or title read.
        await self._command("return id of window id windowId as text")

    async def _command(self, command: str) -> str:
        if self._closed:
            raise LookupError("职达的 Safari 招聘窗口已关闭")
        # Explicitly attached personal windows must remain on the selected tab;
        # switching it stops work, never redirects or silently selects another.
        if self._require_selected_tab:
            command = ('if (index of current tab of window id windowId) is not tabIndex then '
                       'error "ZHIDA_BOUND_TAB_CHANGED"\n' + command)
        script = WINDOW_COMMAND.replace("__COMMAND__", command)
        try:
            async with self.context.lock:
                return await self.context.runner(script, str(self.window_id), str(self.tab_index))
        except LookupError:
            self._closed = True
            raise

    def _argument(self, value: Any) -> str:
        if isinstance(value, SafariHandle):
            if value.page is not self:
                raise ValueError("不能跨招聘窗口复用网页句柄")
            return f"window[{json.dumps(self._registry)}]?.get({json.dumps(value.key)})"
        if isinstance(value, dict):
            return "{" + ",".join(json.dumps(str(k)) + ":" + self._argument(v) for k, v in value.items()) + "}"
        if isinstance(value, (list, tuple)):
            return "[" + ",".join(self._argument(v) for v in value) + "]"
        return json.dumps(value, ensure_ascii=False, allow_nan=False)

    async def _evaluate(self, expression: str, *, handle: bool = False):
        expected_url = None
        if self.url_validator is not None:
            # URL metadata only: reject local/private pages before any DOM,
            # title, storage or form script is executed.
            expected_url = await self.read_url()
            self.url_validator(expected_url)
        key = uuid4().hex if handle else ""
        save = (
            f"(window[{json.dumps(self._registry)}] ||= new Map()).set({json.dumps(key)}, result);"
            "return {kind: result instanceof Element ? 'element' : 'value', present: result != null};"
            if handle else "return result === undefined ? null : result;"
        )
        guard = (f"if (location.href !== {json.dumps(expected_url)}) return JSON.stringify("
                 "{ok:false,url:location.href,title:'',code:'ZHIDA_PAGE_CHANGED'});"
                 if expected_url is not None else "")
        js = f'''(() => {{
          {guard}
          try {{
            const value = (() => {{ {DOM_HELPERS}
              const result = {expression};
              if (result && typeof result.then === 'function') throw new Error('ZHIDA_ASYNC_SCRIPT');
              {save}
            }})();
            return JSON.stringify({{ok:true, url:location.href, title:document.title, value}});
          }} catch(error) {{
            return JSON.stringify({{ok:false, url:location.href, title:document.title,
              code: String(error.message).startsWith('ZHIDA_') ? error.message : 'ZHIDA_SCRIPT_ERROR'}});
          }}
        }})()'''
        raw = await self._command("return do JavaScript " + apple_literal(js) + " in tab tabIndex of window id windowId")
        try:
            result = json.loads(raw)
        except (ValueError, TypeError):
            raise RuntimeError("Safari 未返回有效网页结果；请检查 JavaScript 权限，不会继续填写") from None
        self.url = result.get("url", self.url)
        self._title = result.get("title", "")
        if not result.get("ok"):
            code = result.get("code")
            messages = {
                "ZHIDA_AMBIGUOUS_CONTROL": "网页控件不是唯一匹配，已停止操作，请重新识别",
                "ZHIDA_CONTROL_NOT_EDITABLE": "网页控件隐藏、禁用或只读，未填写",
                "ZHIDA_OPTION_UNAVAILABLE": "网页中不存在唯一可用选项，未选择",
                "ZHIDA_UNSUPPORTED_SELECTOR": "Safari 暂不支持这个控件的定位方式，请在官网核对",
                "ZHIDA_ASYNC_SCRIPT": "Safari 暂不支持这个异步网页操作，未继续",
                "ZHIDA_HANDLE_EXPIRED": "网页已经跳转，旧控件失效，请重新识别",
                "ZHIDA_CHECK_NOT_COMMITTED": "网页没有接受勾选操作，请人工核对",
                "ZHIDA_PAGE_CHANGED": "招聘页在地址检查后发生了跳转，未执行网页操作；请同步当前页面后重试",
            }
            raise ValueError(messages.get(code, "Safari 网页操作未成功，已停止；请重新识别或在官网核对"))
        if handle:
            value = result.get("value") or {}
            return SafariHandle(self, key, value.get("kind") == "element", bool(value.get("present")))
        return result.get("value")

    async def evaluate(self, expression: str, arg=None):
        return await self._evaluate(f"((fn) => typeof fn === 'function' ? fn({self._argument(arg)}) : fn)({expression})")

    async def evaluate_handle(self, expression: str, arg=None):
        return await self._evaluate(f"((fn) => typeof fn === 'function' ? fn({self._argument(arg)}) : fn)({expression})", handle=True)

    async def refresh(self):
        await self.evaluate("() => null")

    async def read_url(self) -> str:
        value = await self._command("return URL of tab tabIndex of window id windowId")
        self.url = value
        return value

    async def title(self):
        await self.refresh()
        return self._title

    async def goto(self, url: str, wait_until="domcontentloaded", timeout=60000):
        await self._command("set URL of tab tabIndex of window id windowId to " + apple_literal(url))
        if url != "about:blank":
            # Setting an Apple Events URL is not a navigation-completion
            # signal. Do not accept the old blank document's readyState.
            await self.wait_for_url(lambda current: current != "about:blank", timeout=timeout)
        await self.wait_for_load_state(wait_until, timeout)

    async def wait_for_timeout(self, milliseconds: int):
        await asyncio.sleep(milliseconds / 1000)

    async def wait_for_load_state(self, state="domcontentloaded", timeout=30000):
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            loaded = await self.evaluate("() => document.readyState")
            if loaded == "complete" or (state == "domcontentloaded" and loaded == "interactive"):
                return
            await asyncio.sleep(0.2)
        raise TimeoutError("Safari 页面尚未加载完成，请检查网络后重试")

    async def wait_for_url(self, predicate, timeout=30000):
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            await self.refresh()
            if predicate(self.url) if callable(predicate) else self.url == predicate:
                return
            await asyncio.sleep(0.2)
        raise TimeoutError("Safari 未进入预期页面，未继续填写")

    async def bring_to_front(self):
        await self._command("set current tab of window id windowId to tab tabIndex of window id windowId\nset index of window id windowId to 1\nactivate")

    def locator(self, selector: str):
        return SafariLocator(self, f"query(document, {json.dumps(selector)})")

    def expect_popup(self, **kwargs):
        # Do not inspect other Safari windows to guess which belongs to the
        # task. Let the user navigate the verified link in this task window.
        raise ValueError("此站点需要新窗口跳转，Safari 暂不自动接管弹窗。请在职达的 Safari 窗口手动进入申请页，再点击‘只读同步当前页’。")

    async def close(self):
        if not self._closed:
            if self._preserve_window:
                self._closed = True
                return
            try:
                await self._command("close window id windowId")
            finally:
                self._closed = True


class SafariHandle:
    def __init__(self, page, key, element=False, present=True):
        self.page, self.key, self.element, self.present = page, key, element, present

    def as_element(self):
        return self if self.element and self.present else None

    async def evaluate(self, expression: str, arg=None):
        ref = self.page._argument(self)
        return await self.page._evaluate(
            f"(() => {{ const el = {ref}; if (el == null) throw new Error('ZHIDA_HANDLE_EXPIRED');"
            f" return ({expression})(el, {self.page._argument(arg)}); }})()"
        )

    async def dispose(self):
        await self.page._evaluate(f"window[{json.dumps(self.page._registry)}]?.delete({json.dumps(self.key)})")


class SafariLocator:
    def __init__(self, page: SafariPage, nodes: str):
        self.page, self.nodes = page, nodes

    @property
    def first(self):
        return self.nth(0)

    @property
    def last(self):
        return SafariLocator(self.page, f"({self.nodes}).slice(-1)")

    def nth(self, index: int):
        return SafariLocator(self.page, f"({self.nodes}).slice({index}, {index + 1})")

    def locator(self, selector: str):
        return SafariLocator(self.page, f"[...new Set(({self.nodes}).flatMap(el => query(el, {json.dumps(selector)})))]")

    def filter(self, *, has=None, has_text=None):
        nodes = self.nodes
        if has is not None:
            if has.page is not self.page:
                raise ValueError("不能跨招聘窗口定位控件")
            # Resolve the child locator relative to the candidate element.
            child = has.nodes.replace("query(document,", "query(el,")
            nodes = f"({nodes}).filter(el => ({child}).length > 0)"
        if has_text is not None:
            if isinstance(has_text, re.Pattern):
                flags = "i" if has_text.flags & re.I else ""
                pattern = has_text.pattern.replace("(?i)", "")
                test = f"new RegExp({json.dumps(pattern)}, {json.dumps(flags)}).test(el.textContent || '')"
            else:
                test = f"(el.textContent || '').includes({json.dumps(str(has_text))})"
            nodes = f"({nodes}).filter(el => {test})"
        return SafariLocator(self.page, nodes)

    async def evaluate(self, expression: str, arg=None):
        return await self.page._evaluate(f"({expression})(one({self.nodes}), {self.page._argument(arg)})")

    async def evaluate_all(self, expression: str, arg=None):
        return await self.page._evaluate(f"({expression})({self.nodes}, {self.page._argument(arg)})")

    async def evaluate_handle(self, expression: str, arg=None):
        return await self.page._evaluate(f"({expression})(one({self.nodes}), {self.page._argument(arg)})", handle=True)

    async def element_handle(self):
        if not await self.count():
            return None
        return await self.evaluate_handle("el => el")

    async def count(self):
        return await self.page._evaluate(f"({self.nodes}).length")

    async def all(self):
        return [self.nth(index) for index in range(await self.count())]

    async def all_text_contents(self):
        return await self.evaluate_all("els => els.map(el => el.textContent || '')")

    async def inner_text(self):
        return await self.evaluate("el => el.innerText || ''")

    async def get_attribute(self, name: str):
        return await self.evaluate("(el, name) => el.getAttribute(name)", name)

    async def input_value(self):
        return await self.evaluate("el => String(el.value ?? '')")

    async def is_visible(self):
        return await self.page._evaluate(f"({self.nodes}).length === 1 && visible(({self.nodes})[0])")

    async def is_disabled(self):
        return await self.evaluate("el => !enabled(el)")

    async def is_editable(self):
        return await self.evaluate("el => enabled(el) && !el.readOnly && visible(el)")

    async def scroll_into_view_if_needed(self, **kwargs):
        # The transport is synchronous. Some engines return a Promise from
        # scrollIntoView; do not accidentally return it as an evaluated result.
        # This performs only a scoped scroll, not a click or value mutation.
        await self.evaluate("el => { el.scrollIntoView({block:'center', inline:'nearest'}); }")

    async def click(self, **kwargs):
        await self.evaluate(r'''el => {
          if (!visible(el) || !enabled(el)) throw new Error('ZHIDA_CONTROL_NOT_EDITABLE');
          el.scrollIntoView({block:'center'});
          // Modern Ant cascaders use an outer .ant-select wrapper for
          // identity but bind opening events to its direct selector child.
          // Dispatch on that proven inner trigger, not the wrapper or any
          // arbitrary descendant (events do not propagate downwards).
          const trigger = el.matches('.ant-select,.ant-cascader') ?
            el.querySelector(':scope > .ant-select-selector') || el : el;
          if (!visible(trigger) || !enabled(trigger)) throw new Error('ZHIDA_CONTROL_NOT_EDITABLE');
          trigger.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
          trigger.dispatchEvent(new MouseEvent('mouseup', {bubbles:true, cancelable:true}));
          trigger.click();
        }''')

    async def fill(self, value: str, **kwargs):
        await self.evaluate(r'''(el, {value, keepFocus}) => {
          writable(el);
          if (!el.matches('input:not([type="file"]):not([type="checkbox"]):not([type="radio"]),textarea'))
            throw new Error('ZHIDA_CONTROL_NOT_EDITABLE');
          el.focus();
          const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
          Object.getOwnPropertyDescriptor(proto, 'value').set.call(el, value);
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
          // Searching a combobox must keep its owned popup open. Normal
          // text fields still blur to commit their validated input.
          if (!keepFocus) el.blur();
        }''', {"value": value, "keepFocus": bool(kwargs.get("keep_focus", False))})

    async def set_checked(self, checked: bool, **kwargs):
        await self.evaluate(r'''(el, checked) => {
          if (!el.matches('input[type="checkbox"],input[type="radio"]') || !enabled(el))
            throw new Error('ZHIDA_CONTROL_NOT_EDITABLE');
          if (el.checked !== checked) el.click();
          if (el.checked !== checked) throw new Error('ZHIDA_CHECK_NOT_COMMITTED');
        }''', bool(checked))

    async def select_option(self, *, label, **kwargs):
        labels = [label] if isinstance(label, str) else list(label)
        return await self.evaluate(r'''(el, labels) => {
          writable(el);
          if (el.tagName !== 'SELECT' || (!el.multiple && labels.length !== 1))
            throw new Error('ZHIDA_OPTION_UNAVAILABLE');
          const choices = labels.map(label => [...el.options].filter(option =>
            option.textContent.trim() === label && !option.disabled && !option.parentElement.disabled));
          if (choices.some(matches => matches.length !== 1)) throw new Error('ZHIDA_OPTION_UNAVAILABLE');
          const selected = new Set(choices.map(matches => matches[0]));
          for (const option of el.options) option.selected = selected.has(option);
          el.dispatchEvent(new Event('input', {bubbles:true}));
          el.dispatchEvent(new Event('change', {bubbles:true}));
          return choices.map(matches => matches[0].value);
        }''', labels)

    async def press(self, key: str, **kwargs):
        if key not in {"Escape", "Enter", "Tab"}:
            raise ValueError("Safari 暂不支持这个按键操作，请在官网完成")
        await self.evaluate(r'''(el, key) => {
          el.focus();
          // Legacy rc/Ant controls read keyCode/which rather than key.
          // These are synthetic events: no native Enter submit default.
          const code = {Escape:27,Enter:13,Tab:9}[key];
          const init = {key, code:key, keyCode:code, which:code, bubbles:true, cancelable:true};
          el.dispatchEvent(new KeyboardEvent('keydown', init));
          el.dispatchEvent(new KeyboardEvent('keyup', init));
          if (key === 'Tab') el.blur();
          // No native Enter default action: never implicitly submit a form.
        }''', key)

    async def wait_for(self, state="visible", timeout=5000):
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            count = await self.count()
            visible = count == 1 and await self.is_visible()
            if ((state == "attached" and count > 0) or (state == "detached" and count == 0)
                    or (state == "visible" and visible) or (state == "hidden" and not visible)):
                return
            await asyncio.sleep(0.15)
        raise TimeoutError("Safari 控件状态未达到预期，未继续操作")

    async def set_input_files(self, *args, **kwargs):
        raise ValueError("Safari 普通窗口暂不支持自动上传附件。请在招聘网页手动选择本次简历，再点击‘只读同步当前页’；未声称附件已上传。")


class SafariKeyboard:
    def __init__(self, page):
        self.page = page

    async def press(self, key):
        if key not in {"Escape", "Tab"}:
            raise ValueError("Safari 不会在未定位控件时按回车或提交表单")
        await self.page.evaluate(r'''key => {
          const el = document.activeElement || document.body;
          const code = {Escape:27,Tab:9}[key];
          const init = {key, code:key, keyCode:code, which:code, bubbles:true, cancelable:true};
          el.dispatchEvent(new KeyboardEvent('keydown', init));
          el.dispatchEvent(new KeyboardEvent('keyup', init));
          if (key === 'Tab') el.blur();
        }''', key)
