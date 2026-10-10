"""Offline one-window product regression. No real Safari/website/model access."""
import asyncio
import json
import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth_models import UserAccount
from app.browser_models import BrowserSnapshot
from app.browser_service import BrowserDemoService
from app.models import CandidateProfile
from app.safari_browser import CREATE_WINDOW_WITH_URL, SafariContext
from app.safari_window_registry import SafariWindowRegistry


class OwnedWindowFixture:
    def __init__(self):
        self.calls = []
        self.creates = 0
        self.permission = False
        self.closed = False
        self.url = "https://jobs.example.test/campus"

    async def __call__(self, script, *args):
        self.calls.append((script, args))
        if script == CREATE_WINDOW_WITH_URL:
            self.creates += 1
            self.closed = False
            self.url = args[0]
            return "271|1|" + args[1]
        assert args == ("271","1"), "Only the registered window and tab may be addressed"
        assert "every tab" not in script and "every window" not in script
        if self.closed:
            raise LookupError("职达记录的窗口已关闭")
        if "return URL of tab tabIndex" in script:
            return self.url
        if "do JavaScript" not in script:
            return "271"
        if not self.permission:
            raise RuntimeError("Safari 尚未允许职达识别网页。允许来自 Apple 事件的 JavaScript")
        value = "complete" if "document.readyState" in script else True
        return json.dumps({"ok":True,"url":self.url,"title":"匿名申请页","value":value})


async def reuse_tests():
    runner = OwnedWindowFixture()
    registry = SafariWindowRegistry(lambda: SafariContext(runner))
    url = runner.url
    first, reused = await registry.open("user-one", url)
    assert not reused and runner.creates == 1
    assert not any("do JavaScript" in script for script, _ in runner.calls), "Opening must not need JS permission"
    again, reused = await registry.open("user-one", url)
    assert reused and again is first and runner.creates == 1
    service = BrowserDemoService()
    service._validate_url = lambda value: value
    service.snapshot = AsyncMock(return_value=BrowserSnapshot(session_id="fixture",url=url,title="fixture",fields=[],browser_engine="safari"))
    with patch.dict(os.environ,{"APP_BROWSER_ENGINE":"safari","APP_BROWSER_HEADLESS":"false"}), \
         patch("app.browser_service.sys.platform","darwin"), \
         patch("app.browser_service.wait_for_rendered_content",AsyncMock()):
        for _ in range(2):
            entry = await registry.for_connection("user-one",url,first.token)
            try:
                await service.start(url,safari_page=entry.page)
                raise AssertionError("Missing JS permission accepted")
            except RuntimeError as exc:
                assert "窗口已保留" in str(exc)
            assert runner.creates == 1 and not first.page.is_closed()
            assert service.session_id is None and service.context is None
            assert registry.windows[first.token] is first
            service.snapshot.assert_not_awaited()
        runner.permission = True
        runner.url = "https://jobs.example.test/application?job=agent"
        entry = await registry.for_connection("user-one",url,first.token)
        await service.start(url,safari_page=entry.page)
        assert runner.creates == 1 and service.page is first.page
        assert service.page.url == runner.url, "Read the user's selected job, not the original homepage"
        assert not any("set URL" in script or "close window" in script for script, _ in runner.calls[1:])
        await service.close()
        assert not first.page.is_closed(), "Disconnecting must retain the normal Safari page"
        await service.start(url,safari_page=(await registry.for_connection("user-one",url)).page)
        assert runner.creates == 1
        await service.close()
    before = len(runner.calls)
    for owner, requested, token in (("another-user",url,first.token), ("user-one",url,"forged-token"),
                                     ("user-one","https://other.example.test/",first.token)):
        try:
            await registry.for_connection(owner,requested,token)
            raise AssertionError("Wrong owner/target/forged token accepted")
        except (LookupError,ValueError):
            pass
    assert len(runner.calls) == before
    runner.closed = True
    try:
        await registry.for_connection("user-one",url,first.token)
        raise AssertionError("Closed window accepted")
    except LookupError:
        pass
    assert runner.creates == 1, "Connecting a closed token must not silently open a new window"
    replacement, reused = await registry.open("user-one",url)
    assert not reused and replacement.token != first.token and runner.creates == 2
    registry.forget_owner("user-one")
    assert not registry.windows

    runner = OwnedWindowFixture()
    registry = SafariWindowRegistry(lambda: SafariContext(runner))
    results = await asyncio.gather(*[registry.open("same-user",runner.url) for _ in range(3)])
    assert runner.creates == 1 and len({entry.token for entry, _ in results}) == 1
    before = len(runner.calls)
    try:
        await registry.for_connection("same-user","https://jobs.example.test/another-role")
        raise AssertionError("Connection without an owned page silently opened another window")
    except LookupError as exc:
        assert "请先点" in str(exc)
    assert runner.creates == 1 and len(runner.calls) == before


def product_api_tests():
    runner = OwnedWindowFixture()
    registry = SafariWindowRegistry(lambda: SafariContext(runner))
    service = BrowserDemoService()
    service._validate_url = lambda value: value
    async def snapshot():
        return BrowserSnapshot(session_id=service.session_id,url=service.page.url,title="匿名申请页",fields=[],browser_engine="safari")
    service.snapshot = AsyncMock(side_effect=snapshot)
    with patch("dotenv.load_dotenv"), patch("sqlite3.connect",side_effect=AssertionError("DB forbidden")):
        from app import main
        fixture = FastAPI()
        fixture.middleware("http")(main.require_account)
        fixture.add_api_route("/api/websites/open",main.open_normal_website,methods=["POST"])
        fixture.add_api_route("/api/browser/start",main.start_browser,methods=["POST"])
        user = UserAccount(id="fixture-user",email="fixture@example.test",created_at=datetime.now(timezone.utc))
        headers = {"origin":"http://127.0.0.1:5173"}
        url = runner.url
        with patch.object(main,"safari_windows",registry), patch.object(main,"browser_demo",service), \
             patch.object(main,"user_for_token",return_value=user), \
             patch.object(main,"get_profile",return_value=CandidateProfile()), \
             patch.object(main,"compose_task_profile",return_value=CandidateProfile()), \
             patch.dict(main.browser_session_owners,{},clear=True), \
             patch.dict(main.browser_task_resumes,{},clear=True), \
             patch.dict(main.browser_task_epochs,{},clear=True), \
             patch.dict(os.environ,{"APP_AUTH_REQUIRED":"true","APP_BROWSER_ENGINE":"safari","APP_BROWSER_HEADLESS":"false"}), \
             patch("app.browser_service.sys.platform","darwin"), \
             patch("app.browser_service.wait_for_rendered_content",AsyncMock()), \
             TestClient(fixture) as client:
            main.app.state.browser_operation_lock = client.portal.call(asyncio.Lock)
            opened = client.post("/api/websites/open",json={"url":url,"browser":"safari"},headers=headers)
            assert opened.status_code == 200, opened.text
            token = opened.json()["safari_window_token"]
            repeated = client.post("/api/websites/open",json={"url":url,"browser":"safari"},headers=headers)
            assert repeated.json()["window_reused"] and repeated.json()["safari_window_token"] == token
            assert runner.creates == 1 and not runner.permission
            payload = {"url":url,"safari_window_token":token}
            for _ in range(2):
                response = client.post("/api/browser/start",json=payload,headers=headers)
                assert response.status_code == 502 and "JavaScript" in response.text
                assert runner.creates == 1 and not main.browser_session_owners
                service.snapshot.assert_not_awaited()
            assert client.post("/api/browser/start",json={**payload,"safari_window_token":"foreign"},headers=headers).status_code == 404
            assert client.post("/api/browser/start",json={**payload,"window_id":271},headers=headers).status_code == 422
            runner.permission = True
            runner.url = "https://jobs.example.test/application?job=agent"
            connected = client.post("/api/browser/start",json=payload,headers=headers)
            assert connected.status_code == 200, connected.text
            assert connected.json()["url"] == runner.url and runner.creates == 1
            assert main.browser_session_owners[connected.json()["session_id"]] == user.id
            assert client.post("/api/websites/open",json={"url":url,"browser":"safari"},headers=headers).status_code == 409
            assert runner.creates == 1
            assert not any("set URL" in script or "close window" in script for script, _ in runner.calls[1:])
    print("safari_window_reuse_test: OK (open→permission failure→retry→same-window connect; no reload, owner/token/target/closed/concurrency guards)")


if __name__ == "__main__":
    asyncio.run(reuse_tests())
    product_api_tests()
