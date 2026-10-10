"""Remember task-created windows or explicitly confirmed existing Safari tabs.

No personal-tab enumeration, implicit front-window adoption, or DOM inspection
during opening/attachment preview. Connection capability is private to its user.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from secrets import token_urlsafe
from time import monotonic

from .external_browser import validate_external_url
from .safari_browser import CAPTURE_EXISTING_WINDOW, SafariContext, SafariPage


@dataclass
class PendingSafariAttachment:
    owner: str
    requested_url: str
    page: SafariPage
    expires_at: float


@dataclass
class OpenedSafariWindow:
    token: str
    owner: str
    requested_url: str
    page: SafariPage


class SafariWindowRegistry:
    def __init__(self, context_factory=SafariContext):
        self.context_factory = context_factory
        self.windows: dict[str, OpenedSafariWindow] = {}
        self.pending: dict[str, PendingSafariAttachment] = {}
        # Python 3.9 must not bind an asyncio primitive at module import time.
        self._lock: asyncio.Lock | None = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _lookup(self, owner: str, url: str, token: str = "") -> OpenedSafariWindow | None:
        if token:
            entry = self.windows.get(token)
            if not entry or entry.owner != owner:
                raise LookupError("这个 Safari 窗口不属于当前账号，或后端已重启；未接管其他窗口")
            if entry.requested_url != url:
                raise ValueError("网址已改变，请先用 Safari 打开本次目标，未连接旧窗口")
            if entry.page.is_closed():
                raise LookupError("职达记录的 Safari 窗口已关闭，请重新打开本次目标")
            return entry
        return next((entry for entry in reversed(list(self.windows.values()))
                     if entry.owner == owner and entry.requested_url == url and not entry.page.is_closed()), None)

    async def open(self, owner: str, url: str) -> tuple[OpenedSafariWindow, bool]:
        url = validate_external_url(url)
        async with self.lock:
            entry = self._lookup(owner, url)
            if entry:
                try:
                    await entry.page.ensure_available()
                except LookupError:
                    self.windows.pop(entry.token, None)
                else:
                    # Do not navigate/reload: preserve login, selected job and draft.
                    await entry.page.bring_to_front()
                    return entry, True
            context = self.context_factory()
            page = await context.new_page(url)
            entry = OpenedSafariWindow(token_urlsafe(32), owner, url, page)
            # Cap only metadata, never close or inspect old browser windows.
            owned = [token for token, value in self.windows.items() if value.owner == owner]
            for token in owned[:-7]:
                self.windows.pop(token, None)
            self.windows[entry.token] = entry
            return entry, False

    async def for_connection(self, owner: str, url: str, token: str = "") -> OpenedSafariWindow:
        url = validate_external_url(url)
        if token:
            entry = self._lookup(owner, url, token)
            await entry.page.ensure_available()
            return entry
        # Refresh/legacy clients may reuse an existing owned capability, but
        # connecting is never permission to open an additional browser page.
        entry = self._lookup(owner, url)
        if not entry:
            raise LookupError("还没有职达创建的招聘页连接记录。请先点‘用 Safari 打开招聘网站’，确认页面后再连接；手动打开的其他窗口不会被自动接管")
        await entry.page.ensure_available()
        return entry

    async def preview_existing(self, owner: str, url: str) -> tuple[str, str]:
        """Called only after the user opts in and selects a front recruitment tab."""
        url = validate_external_url(url)
        async with self.lock:
            context = self.context_factory()
            raw = await context.runner(CAPTURE_EXISTING_WINDOW, url)
            parts = raw.split('|', 2)
            if (len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit()
                    or int(parts[0]) < 1 or int(parts[1]) < 1 or parts[2] != url):
                raise ValueError('未取得与目标网址一致的当前标签页，未连接任何页面')
            page = SafariPage(context, int(parts[0]), int(parts[1]))
            page.url = url
            page._preserve_window = True
            page._require_selected_tab = True
            if any(entry.owner != owner and entry.page.window_id == page.window_id
                   for entry in self.windows.values()):
                raise ValueError('这个 Safari 窗口已有其他账号的任务，未连接')
            for key in [key for key, value in self.pending.items()
                        if value.expires_at <= monotonic() or value.owner == owner]:
                self.pending.pop(key, None)
            token = token_urlsafe(32)
            self.pending[token] = PendingSafariAttachment(owner, url, page, monotonic() + 120)
            return token, url

    async def confirm_existing(self, owner: str, url: str, preview_token: str) -> OpenedSafariWindow:
        url = validate_external_url(url)
        async with self.lock:
            pending = self.pending.get(preview_token)
            if (not pending or pending.owner != owner or pending.requested_url != url
                    or pending.expires_at <= monotonic()):
                raise LookupError('窗口确认已过期或不属于当前账号，请重新选择目标窗口')
            # URL/selected-tab checks are metadata only; no DOM, cookies or
            # browser values are read before the separate analysis request.
            if await pending.page.read_url() != url:
                self.pending.pop(preview_token, None)
                raise ValueError('确认期间招聘页已跳转，未连接；请核对填写链接后重试')
            if any(entry.owner != owner and entry.page.window_id == pending.page.window_id
                   for entry in self.windows.values()):
                raise ValueError('这个 Safari 窗口已有其他账号的任务，未连接')
            self.pending.pop(preview_token, None)
            # Reuse an already registered capability instead of accumulating
            # two independently usable handles for the same task window.
            for entry in self.windows.values():
                if (entry.owner == owner and entry.page.window_id == pending.page.window_id
                        and entry.page.tab_index == pending.page.tab_index and not entry.page.is_closed()):
                    entry.page._preserve_window = True
                    entry.page._require_selected_tab = True
                    entry.requested_url = url
                    return entry
            entry = OpenedSafariWindow(token_urlsafe(32), owner, url, pending.page)
            pending.page.context.pages.append(pending.page)
            owned = [key for key, value in self.windows.items() if value.owner == owner]
            for key in owned[:-7]:
                self.windows.pop(key, None)
            self.windows[entry.token] = entry
            return entry

    def forget_owner(self, owner: str) -> None:
        for token in [token for token, entry in self.windows.items() if entry.owner == owner]:
            self.windows.pop(token, None)
        for token in [token for token, entry in self.pending.items() if entry.owner == owner]:
            self.pending.pop(token, None)


safari_windows = SafariWindowRegistry()
