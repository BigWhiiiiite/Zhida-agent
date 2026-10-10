"""User-requested normal browser launch; no DOM access or browser automation."""
from __future__ import annotations

import asyncio
import ipaddress
import re
import sys
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, StrictStr


class ExternalWebsiteOpen(BaseModel):
    model_config = {"extra": "forbid"}
    url: StrictStr = Field(min_length=1, max_length=2000)
    browser: Literal["safari", "chrome"] = "safari"


class ExistingSafariPreview(BaseModel):
    model_config = {"extra": "forbid"}
    url: StrictStr = Field(min_length=1, max_length=2000)


class ExistingSafariConfirmation(ExistingSafariPreview):
    preview_token: StrictStr = Field(min_length=20, max_length=100)


def validate_external_url(raw: str) -> str:
    """Lexical public-domain validation; no server HTTP/DNS requests.

    Normal browser navigation is performed by the user's browser, not a server
    fetch. Avoid requiring server DNS to agree with the user's normal browser.
    The stricter resolved-IP guard for automatic navigation stays unchanged.
    """
    value = raw.strip()
    if not value or len(value) > 2000 or re.search(r"[\x00-\x20\x7f\\]", value):
        raise ValueError("请使用不含空白或控制字符的完整招聘网址")
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError("只接受不含账号密码的 HTTP(S) 招聘网址")
        if "%" in parsed.netloc or parsed.port not in {None, 80, 443}:
            raise ValueError("请使用公开招聘域名及标准端口")
    except ValueError:
        raise ValueError("请使用不含账号密码的完整公开 HTTP(S) 招聘网址") from None
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("不能使用 IP 地址作为招聘网址")
    forbidden = (".localhost", ".local", ".internal", ".intranet", ".lan", ".home", ".home.arpa")
    labels = host.split(".")
    if (host in {"localhost", "metadata", "instance-data"} or host.endswith(forbidden)
            or len(labels) < 2 or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in labels)
            or not re.fullmatch(r"(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})", labels[-1])):
        raise ValueError("请使用完整公开招聘域名，不能使用本机、内网或混淆地址")
    return value


async def open_external_website(payload: ExternalWebsiteOpen) -> dict[str, str | bool]:
    url = validate_external_url(payload.url)
    if sys.platform != "darwin":
        raise ValueError("此本机浏览器启动入口目前仅支持 macOS；请复制网址到自己的浏览器打开")
    application = {"safari": "Safari", "chrome": "Google Chrome"}[payload.browser]
    # Fixed executable and application whitelist, URL as one literal argv;
    # never a shell, Apple Events script, remote debugging port or new profile.
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/open", "-a", application, url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        await asyncio.wait_for(process.communicate(), timeout=10)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if process.returncode is None:
            process.kill()
        await process.wait()
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise RuntimeError("未确认系统已接受打开请求；请先检查浏览器窗口，不会自动重复打开") from None
    if process.returncode != 0:
        # Never expose launcher stderr (paths, URLs, arguments or other data).
        raise RuntimeError(f"系统未接受 {application} 打开请求，请确认已安装该浏览器，或复制网址手动打开")
    return {"status": "requested", "browser": payload.browser, "automation_connected": False}
