"""Run existing executor/assist suites with strict offline browser defaults.

The old suites own their anonymous HTML fixtures. This harness supplies a
deny-all fallback route, blocks workers/sockets, and prohibits dotenv/SQLite.
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path
from unittest.mock import patch

from playwright.async_api import Browser


SUITES = (
    "browser_assist_capabilities_test.py",
    "target_identity_boundary_test.py",
    "execute_resume_upload_test.py",
    "application_assist_browser_test.py",
    "browser_smoke_test.py",
)
new_context = Browser.new_context


async def offline_context(browser, *args, **kwargs):
    kwargs["service_workers"] = "block"
    context = await new_context(browser, *args, **kwargs)
    await context.route("**/*", lambda route: route.abort())
    await context.route_web_socket("**/*", lambda socket: socket.close())
    return context


def run():
    names = sys.argv[1:] or SUITES
    assert all(name in SUITES for name in names), names
    with patch.object(Browser, "new_context", offline_context), \
         patch("dotenv.load_dotenv", side_effect=AssertionError("dotenv forbidden")), \
         patch("sqlite3.connect", side_effect=AssertionError("database forbidden")):
        for name in names:
            runpy.run_path(str(Path(__file__).with_name(name)), run_name="__main__")
    print("execute_isolated_regressions_test: OK (" + ", ".join(names) + ")")


if __name__ == "__main__":
    run()
