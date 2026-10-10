"""Offline URL/DNS regressions; no live DNS, Safari, website or model access."""
import asyncio
import os
import socket
from unittest.mock import AsyncMock, patch

from app.browser_models import BrowserSnapshot
from app.browser_service import BrowserDemoService
from app.safari_browser import SafariContext
from app.safari_window_registry import SafariWindowRegistry
from safari_window_reuse_test import OwnedWindowFixture


URL = "https://eoap.cebbank.com/uiap/wt/CEB/zpzh/campus?private_marker=not-for-errors#apply"


def records(*addresses):
    return [(socket.AF_INET6 if ":" in address else socket.AF_INET,
             socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]


def validation_tests():
    with patch("app.browser_service.socket.getaddrinfo", return_value=records("1.202.217.21", "240e:604:204:800:1110::105")) as dns:
        assert BrowserDemoService._validate_url(URL) == URL
        assert dns.call_args.kwargs == {"type": socket.SOCK_STREAM}
    for addresses, expected in (
        (("127.0.0.1",), "本机、内网或保留地址"),
        (("10.0.0.1",), "本机、内网或保留地址"),
        (("169.254.169.254",), "本机、内网或保留地址"),
        (("100.64.0.1",), "本机、内网或保留地址"),
        (("224.0.0.1",), "本机、内网或保留地址"),
        (("::1",), "本机、内网或保留地址"),
        (("::ffff:127.0.0.1",), "本机、内网或保留地址"),
        (("198.18.0.2",), "Fake-IP"),
        (("198.19.255.254",), "Fake-IP"),
        (("1.202.217.21", "192.168.1.1"), "本机、内网或保留地址"),
        ((), "没有返回可验证的地址"),
    ):
        with patch("app.browser_service.socket.getaddrinfo", return_value=records(*addresses)):
            try:
                BrowserDemoService._validate_url(URL)
                raise AssertionError(f"Non-public DNS accepted: {addresses}")
            except ValueError as exc:
                assert expected in str(exc) and "eoap.cebbank.com" in str(exc)
                assert "private_marker" not in str(exc) and "not-for-errors" not in str(exc)
    with patch("app.browser_service.socket.getaddrinfo", side_effect=socket.gaierror("private resolver details")):
        try:
            BrowserDemoService._validate_url(URL)
            raise AssertionError("Unresolved host accepted")
        except ValueError as exc:
            assert "后端 DNS" in str(exc) and "private resolver details" not in str(exc)
    for url in ("http://127.0.0.1:5173/", "http://localhost:5173/", "http://[::1]:5173/"):
        with patch("app.browser_service.socket.getaddrinfo", side_effect=AssertionError("Local URL must stop before DNS")):
            try:
                BrowserDemoService._validate_url(url)
                raise AssertionError("Frontend/private tab accepted")
            except ValueError as exc:
                assert "本机页面" in str(exc) and "切回招聘网站" in str(exc)
    for url in ("https://jobs.local/", "https://jobs.internal/", "https://jobs.example.test:8443/",
                "https://user:password@jobs.example.test/", "http://2130706433/", "file:///private/data"):
        with patch("app.browser_service.socket.getaddrinfo", side_effect=AssertionError("Invalid URL must stop before DNS")):
            try:
                BrowserDemoService._validate_url(url)
                raise AssertionError("Unsafe URL accepted")
            except ValueError:
                pass


async def same_window_tests():
    runner = OwnedWindowFixture()
    runner.permission = True
    registry = SafariWindowRegistry(lambda: SafariContext(runner))
    entry, _ = await registry.open("fixture-user", runner.url)
    service = BrowserDemoService()
    service.snapshot = AsyncMock(return_value=BrowserSnapshot(
        session_id="fixture", url=runner.url, title="fixture", fields=[], browser_engine="safari"))
    host_calls = []

    def dns(host, *args, **kwargs):
        host_calls.append(host)
        return records("10.0.0.1" if host == "redirect.example.test" else "1.202.217.21")

    with patch.dict(os.environ, {"APP_BROWSER_ENGINE": "safari", "APP_BROWSER_HEADLESS": "false"}), \
         patch("app.browser_service.sys.platform", "darwin"), \
         patch("app.browser_service.socket.getaddrinfo", side_effect=dns), \
         patch("app.browser_service.wait_for_rendered_content", AsyncMock()) as rendered:
        for current_url, expected in (("http://127.0.0.1:5173/", "本机页面"),
                                      ("https://redirect.example.test/login?secret=not-for-errors", "redirect.example.test")):
            runner.url = current_url
            count = len(runner.calls)
            try:
                await service.start(entry.requested_url, safari_page=entry.page)
                raise AssertionError("Private redirect/tab accepted")
            except RuntimeError as exc:
                assert "检查职达绑定的 Safari 招聘页" in str(exc) and expected in str(exc)
                assert "secret=" not in str(exc) and "not-for-errors" not in str(exc)
            assert runner.creates == 1 and not entry.page.is_closed()
            assert service.session_id is None and service.context is None
            service.snapshot.assert_not_awaited()
            rendered.assert_not_awaited()
            assert not any("do JavaScript" in script for script, _ in runner.calls[count:]), "Unsafe bound tab must stop before DOM access"
        assert "127.0.0.1" not in host_calls
        runner.url = "https://jobs.example.test/application?job=agent"
        await service.start(entry.requested_url, safari_page=entry.page)
        service.snapshot.assert_awaited_once()
        assert runner.creates == 1 and service.page is entry.page
        assert not any("set URL" in script or "close window" in script for script, _ in runner.calls[1:])
        await service.close()
        assert not entry.page.is_closed()


if __name__ == "__main__":
    validation_tests()
    asyncio.run(same_window_tests())
    print("browser_url_security_test: OK (public DNS, private/mixed/fake-IP rejection, precise errors, same-window retry)")
