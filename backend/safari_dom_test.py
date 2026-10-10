"""Execute generated adapter scripts in an offline Node VM, never Safari."""
import asyncio
import json
import os
import re
import shutil
from pathlib import Path

from app.safari_browser import CREATE_WINDOW, SafariContext


async def run():
    node = os.getenv("ZHIDA_TEST_NODE") or shutil.which("node")
    if not node:
        raise RuntimeError("Offline DOM test needs Node; set ZHIDA_TEST_NODE to its executable")
    process = await asyncio.create_subprocess_exec(node, str(Path(__file__).with_name("safari_dom_fixture.mjs")),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    calls = []

    async def runner(script, *args):
        calls.append(script)
        if script == CREATE_WINDOW:
            return "81|1|" + args[0]
        assert args == ("81","1")
        if "close window id" in script:
            return ""
        match = re.search(r'return do JavaScript (".*") in tab tabIndex', script)
        assert match, "Unexpected transport command"
        javascript = json.loads(match[1])
        process.stdin.write((json.dumps({"script": javascript}) + "\n").encode())
        await process.stdin.drain()
        reply = json.loads(await asyncio.wait_for(process.stdout.readline(), 4))
        assert "error" not in reply, reply
        return reply["result"]

    try:
        context = SafariContext(runner)
        page = await context.new_page()
        name = page.locator('input[name="candidate"]')
        assert await name.count() == 1
        events = await name.evaluate("el => { const events=[]; el.addEventListener('input',()=>events.push('input')); el.addEventListener('change',()=>events.push('change')); el._events=events; return events; }")
        assert events == []
        value = '匿名姓名 " \\ \n中英 Mixed'
        await name.fill(value)
        assert await name.input_value() == value
        assert await name.evaluate('el => el._events') == ['input', 'change']
        notes = page.locator('textarea[name="notes"]')
        await notes.fill('匿名项目描述')
        assert await notes.input_value() == '匿名项目描述'

        city = page.locator('select[name="city"]')
        assert await city.select_option(label='北京') == ['1']
        assert await city.input_value() == '1'
        assert await city.locator('option').all_text_contents() == ['请选择', '北京', '上海']
        try:
            await city.select_option(label='不存在的城市')
            raise AssertionError('Unknown option selected')
        except ValueError:
            pass
        assert await city.input_value() == '1'

        checkbox = page.locator('input[name="confirmed"]')
        await checkbox.set_checked(True)
        assert await checkbox.evaluate('el => el.checked') is True
        await checkbox.set_checked(False)
        assert await checkbox.evaluate('el => el.checked') is False
        await page.locator('input[name="gender"]').set_checked(True)
        assert await page.locator('input[name="gender"]').evaluate('el => el.checked') is True
        await name.evaluate('el => el.disabled=true')
        try:
            await name.fill('must-not-write')
            raise AssertionError('Disabled field written')
        except ValueError:
            pass
        assert await name.input_value() == value
        await name.evaluate('el => el.disabled=false')
        try:
            await page.locator('input').fill('ambiguous')
            raise AssertionError('Ambiguous controls written')
        except ValueError:
            pass
        assert await name.input_value() == value
        assert await page.locator('#record').filter(has=name).count() == 1

        handle = await name.element_handle()
        assert handle.as_element() is handle
        assert await city.evaluate('(el, other) => other.value', handle) == value
        await handle.dispose()
        try:
            await handle.evaluate('el => el.value')
            raise AssertionError('Disposed handle reused')
        except ValueError:
            pass
        cleanup = await name.evaluate_handle('el => () => { el.datasetFlag="cleaned"; }')
        assert cleanup.as_element() is None
        await cleanup.evaluate('fn => fn()')
        await cleanup.dispose()
        assert await name.evaluate('el => el.datasetFlag') == 'cleaned'
        await name.evaluate('el => { el.keys=[]; el.addEventListener("keydown",e=>el.keys.push(e.key)); }')
        await name.press('Enter')
        assert await name.evaluate('el => el.keys') == ['Enter']
        await name.evaluate('el => { el.codes=[]; el.addEventListener("keydown",e=>el.codes.push([e.keyCode,e.which])); }')
        await name.press('Enter')
        await name.press('Escape')
        await page.keyboard.press('Escape')
        assert await name.evaluate('el => el.codes') == [[13,13],[27,27],[27,27]]
        assert not any('.submit()' in script or 'requestSubmit(' in script for script in calls)
        await context.close()
        print('safari_dom_test: OK (input events, exact options, checks, disabled/ambiguous guards, handles, no implicit submit)')
    finally:
        process.stdin.close()
        await process.wait()


if __name__ == '__main__':
    asyncio.run(run())
