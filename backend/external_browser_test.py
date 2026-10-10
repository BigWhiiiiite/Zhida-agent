"""Offline ordinary-launch tests. Never open a real browser or read real tabs."""
import asyncio
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.external_browser import ExternalWebsiteOpen, open_external_website, validate_external_url
from app.auth_models import UserAccount


async def transport_tests():
    urls = ["https://eoap.cebbank.com/uiap/wt/CEB/zpzh/campus",
            "https://eoap.cebbank.com/uiap/wt/CEB/zpzhm/campus",
            "https://jobs.example.test/job?key=a%26b#apply"]
    for url in urls:
        assert validate_external_url(url) == url
    for url in ("javascript:alert(1)", "file:///etc/passwd", "https://user:password@jobs.example.test/",
                "https://127.0.0.1/", "http://localhost", "http://[::1]/", "http://2130706433/",
                "http://0x7f000001/", "https://jobs.local/", "https://jobs.internal/",
                "https://jobs.lan/", "https://jobs.example.test:8443/", "https://jobs.example.test/a b",
                "https://jobs.example.test\\path", "https://%65xample.com/", "--args", "https://jobs.example.test/\n"):
        # Outer whitespace is trimmed, as with the frontend; controls inside are forbidden.
        if url.endswith("\n"):
            url = "https://jobs.example.test/a\nb"
        try:
            validate_external_url(url)
            raise AssertionError("Unsafe URL accepted")
        except ValueError:
            pass
    for browser, application in (("safari", "Safari"), ("chrome", "Google Chrome")):
        process = AsyncMock()
        process.returncode = 0
        process.communicate.return_value = (b"", b"")
        with patch("app.external_browser.sys.platform", "darwin"), \
             patch("app.external_browser.asyncio.create_subprocess_exec", AsyncMock(return_value=process)) as spawn, \
             patch("socket.getaddrinfo", side_effect=AssertionError("Normal opening must not depend on server DNS")):
            result = await open_external_website(ExternalWebsiteOpen(url=urls[0], browser=browser))
            assert result == {"status":"requested", "browser":browser, "automation_connected":False}
            assert spawn.await_args.args == ("/usr/bin/open", "-a", application, urls[0])
            assert "shell" not in spawn.await_args.kwargs
    process = AsyncMock()
    process.returncode = 1
    process.communicate.return_value = (b"", b"private launcher data")
    with patch("app.external_browser.sys.platform", "darwin"), \
         patch("app.external_browser.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
        try:
            await open_external_website(ExternalWebsiteOpen(url=urls[0]))
            raise AssertionError("Failed launch claimed success")
        except RuntimeError as exc:
            assert "private" not in str(exc)
    process = AsyncMock()
    process.returncode = None
    process.kill = Mock()
    with patch("app.external_browser.sys.platform", "darwin"), \
         patch("app.external_browser.asyncio.create_subprocess_exec", AsyncMock(return_value=process)), \
         patch("app.external_browser.asyncio.wait_for", side_effect=asyncio.TimeoutError):
        # Close the synthetic coroutine because a mocked wait_for never awaits it.
        process.communicate = lambda: None
        try:
            await open_external_website(ExternalWebsiteOpen(url=urls[0]))
            raise AssertionError("Timeout claimed success")
        except RuntimeError:
            process.kill.assert_called_once()
            process.wait.assert_awaited_once()
    with patch("app.external_browser.sys.platform", "linux"), \
         patch("app.external_browser.asyncio.create_subprocess_exec", AsyncMock()) as spawn:
        try:
            await open_external_website(ExternalWebsiteOpen(url=urls[0]))
            raise AssertionError("Unsupported platform accepted")
        except ValueError:
            spawn.assert_not_awaited()


def api_tests():
    with patch("dotenv.load_dotenv"), patch("sqlite3.connect", side_effect=AssertionError("DB forbidden")):
        from app import main
        fixture = FastAPI()
        fixture.middleware("http")(main.require_account)
        fixture.add_api_route("/api/websites/open", main.open_normal_website, methods=["POST"])
        user = UserAccount(id="fixture-user", email="fixture@example.test", created_at=datetime.now(timezone.utc))
        payload = {"url":"https://jobs.example.test/campus", "browser":"chrome"}
        headers = {"origin":"http://127.0.0.1:5173", "sec-fetch-site":"same-site"}
        with patch.object(main, "user_for_token", return_value=user), \
             patch.dict(os.environ, {"APP_AUTH_REQUIRED":"true"}), \
             patch.object(main, "open_external_website", AsyncMock(return_value={"status":"requested","browser":"chrome","automation_connected":False})) as launch, \
             TestClient(fixture) as client:
            response = client.post("/api/websites/open", json=payload, headers=headers)
            assert response.status_code == 200, response.text
            launch.assert_awaited_once()
            assert response.json()["automation_connected"] is False
            for origin in ("https://evil.example.com", "null", ""):
                launch.reset_mock()
                invalid_headers = {"origin":origin} if origin else {}
                assert client.post("/api/websites/open",json=payload,headers=invalid_headers).status_code == 403
                launch.assert_not_awaited()
            launch.reset_mock()
            assert client.post("/api/websites/open",json=payload,headers={**headers,"sec-fetch-site":"cross-site"}).status_code == 403
            for invalid in ({}, {**payload,"browser":"Terminal"}, {**payload,"script":"synthetic"}, {**payload,"url":1}):
                assert client.post("/api/websites/open",json=invalid,headers=headers).status_code == 422
            launch.assert_not_awaited()
            with patch.object(main,"user_for_token",return_value=None):
                assert client.post("/api/websites/open",json=payload,headers=headers).status_code == 401
            request = Request({"type":"http","client":("203.0.113.2",1000),"headers":[(b"origin",headers["origin"].encode())]})
            try:
                asyncio.run(main.open_normal_website(ExternalWebsiteOpen(**payload), request))
                raise AssertionError("Remote client allowed local app launch")
            except HTTPException as exc:
                assert exc.status_code == 403
            launch.assert_not_awaited()
    print("external_browser_test: OK (normal app argv, no DOM/DNS, truthful state, local/auth/origin/strict-payload guards)")


if __name__ == "__main__":
    asyncio.run(transport_tests())
    api_tests()
