"""Persistent browser-login and per-user isolation smoke test."""
from __future__ import annotations

import asyncio
import os
from tempfile import TemporaryDirectory

from app.browser_service import BrowserDemoService


async def main() -> None:
    previous = {
        name: os.environ.get(name)
        for name in ("APP_BROWSER_PROFILE_DIR", "APP_BROWSER_HEADLESS", "APP_BROWSER_PERSISTENT")
    }
    with TemporaryDirectory() as temporary:
        os.environ["APP_BROWSER_PROFILE_DIR"] = temporary
        os.environ["APP_BROWSER_HEADLESS"] = "true"
        os.environ["APP_BROWSER_PERSISTENT"] = "true"
        try:
            first = BrowserDemoService()
            first._validate_url = lambda value: value  # type: ignore[method-assign]
            await first.start("about:blank", "candidate-a")
            assert first.context is not None
            await first.context.add_cookies([{
                "name": "moka_login", "value": "remembered", "domain": "app.mokahr.com", "path": "/",
                "expires": 2_000_000_000,
            }])
            await first.close()

            reopened = BrowserDemoService()
            reopened._validate_url = lambda value: value  # type: ignore[method-assign]
            await reopened.start("about:blank", "candidate-a")
            assert reopened.context is not None
            cookies = await reopened.context.cookies("https://app.mokahr.com")
            assert any(item["name"] == "moka_login" and item["value"] == "remembered" for item in cookies)
            await reopened.close()

            isolated = BrowserDemoService()
            isolated._validate_url = lambda value: value  # type: ignore[method-assign]
            await isolated.start("about:blank", "candidate-b")
            assert isolated.context is not None
            assert not await isolated.context.cookies("https://app.mokahr.com")
            assert isolated.profile_directory("candidate-a") != isolated.profile_directory("candidate-b")
            await isolated.close()
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


asyncio.run(main())
print("Persistent browser profile smoke test passed")
