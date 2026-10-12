"""Offline cleanup transport/JS regression; never launch or inspect Safari."""
import asyncio
import json
import os
import re
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from app.safari_browser import SafariContext, SafariHandle, SafariPage


URL = "https://synthetic.invalid/application?job=anonymous#form"


def fixture():
    runner = AsyncMock(return_value="false")
    context = SafariContext(runner)
    page = SafariPage(context, 81, 2)
    page.url = URL
    page._require_selected_tab = True
    page._evaluate = AsyncMock(side_effect=AssertionError("generic evaluation must not clean up"))
    handle = SafariHandle(page, "random-synthetic-handle")
    return page, handle, runner


def test_cleanup_exact_origin_guard_precedes_script_and_never_reads_title():
    page, handle, runner = fixture()
    page.url = "https://another.invalid/home"  # Cache changes are not authority.
    asyncio.run(handle.dispose())
    assert runner.await_count == 1
    script, window_id, tab_index = runner.await_args.args
    assert (window_id, tab_index) == ("81", "2")
    assert "index of current tab" in script
    assert script.index("if (URL of tab") < script.index("return do JavaScript")
    assert URL in script
    assert "another.invalid" not in script
    assert "document.title" not in script and "document.body" not in script
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "registry.__zhidaDocument !== document" in script
    page._evaluate.assert_not_awaited()
    asyncio.run(handle.dispose())
    assert runner.await_count == 1, "cleanup is idempotent"
    with pytest.raises(ValueError, match="已释放"):
        asyncio.run(handle.evaluate("el => el"))


@pytest.mark.parametrize("change", ["context", "window", "tab", "registry", "closed"])
def test_changed_binding_does_not_issue_any_cleanup_apple_event(change):
    page, handle, runner = fixture()
    if change == "context":
        page.context = SafariContext()
    elif change == "window":
        page.window_id = 82
    elif change == "tab":
        page.tab_index = 1
    elif change == "registry":
        page._registry = "replacement-registry"
    else:
        page._closed = True
    asyncio.run(handle.dispose())
    runner.assert_not_awaited()
    page._evaluate.assert_not_awaited()


def test_different_url_metadata_guard_stops_before_any_cleanup_javascript():
    page, handle, runner = fixture()
    execution = []
    async def moved(script, *args):
        # Model the AppleScript branch, not a browser or a transport fallback.
        prefix = script[:script.index("return do JavaScript")]
        assert "if (URL of tab tabIndex of window id windowId) is not" in prefix
        assert 'then return "ZHIDA_HANDLE_CLEANUP_SKIPPED"' in prefix
        execution.append("metadata-only-stop")
        return "ZHIDA_HANDLE_CLEANUP_SKIPPED"
    runner.side_effect = moved
    asyncio.run(handle.dispose())
    assert execution == ["metadata-only-stop"]
    page._evaluate.assert_not_awaited()


@pytest.mark.parametrize("scenario,deleted", [
    ("same-document", True), ("same-url-reload", False),
    ("copied-old-registry", False), ("url-race", False),
])
def test_cleanup_js_only_deletes_from_actual_original_document(scenario, deleted):
    node = os.getenv("ZHIDA_TEST_NODE") or shutil.which("node")
    assert node, "offline cleanup script verification requires Node"
    page, handle, runner = fixture()
    asyncio.run(handle.dispose())
    command = runner.await_args.args[0]
    match = re.search(r'return do JavaScript (".*") in tab tabIndex', command)
    assert match
    script = json.loads(match[1])
    # Execute only the generated cleanup expression in a deterministic Node VM.
    # A title getter throws so accidental replacement-page content reads fail.
    harness = r'''
const vm = require('node:vm');
let raw = '';
process.stdin.on('data', data => raw += data);
process.stdin.on('end', () => {
  const input = JSON.parse(raw);
  let titleReads = 0;
  const original = {};
  const replacement = {};
  for (const doc of [original, replacement]) Object.defineProperty(doc, 'title', {
    get() { titleReads++; throw new Error('title must not be read'); }
  });
  const registry = new Map([[input.key, original]]);
  Object.defineProperty(registry, '__zhidaDocument', {value: original});
  const window = input.scenario === 'same-url-reload' ? {} : {[input.registry]: registry};
  const document = ['same-url-reload', 'copied-old-registry'].includes(input.scenario) ? replacement : original;
  const href = input.scenario === 'url-race' ? 'https://other.invalid/private' : input.url;
  const result = vm.runInNewContext(input.script, {Map, window, document, location: {href}}, {timeout: 1000});
  process.stdout.write(JSON.stringify({result, deleted: !registry.has(input.key), titleReads}));
});
'''
    payload = {"script": script, "key": handle.key, "registry": page._registry,
               "url": URL, "scenario": scenario}
    result = subprocess.run([node, "-e", harness], input=json.dumps(payload),
                            capture_output=True, text=True, check=True, timeout=4)
    observed = json.loads(result.stdout)
    assert observed == {"result": deleted, "deleted": deleted, "titleReads": 0}
