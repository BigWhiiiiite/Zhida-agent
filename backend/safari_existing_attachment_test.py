"""Anonymous offline fixtures; no real browser, model or personal data."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.external_browser import ExistingSafariConfirmation, ExistingSafariPreview
from app.safari_browser import CAPTURE_EXISTING_WINDOW, SafariContext
from app.safari_window_registry import SafariWindowRegistry


URL = 'https://jobs.example.test/resumeEdit?postId=fixture'


class ExistingWindow:
    def __init__(self):
        self.url, self.tab, self.closed, self.calls = URL, 1, False, []

    async def __call__(self, script, *args):
        self.calls.append((script, args))
        assert 'every tab' not in script and 'every window' not in script
        assert 'do JavaScript' not in script and 'set URL' not in script
        assert 'make new document' not in script and 'close window' not in script
        if script == CAPTURE_EXISTING_WINDOW:
            if self.url != args[0]:
                raise ValueError('地址不一致')
            return f'271|{self.tab}|{self.url}'
        assert args == ('271', '1')
        if self.closed:
            raise LookupError('窗口关闭')
        if self.tab != 1:
            assert 'index of current tab' in script
            raise ValueError('标签页改变')
        return self.url if 'return URL' in script else '271'


class ExistingAttachment(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runner = ExistingWindow()
        self.registry = SafariWindowRegistry(lambda: SafariContext(self.runner))

    async def test_two_phase_confirmation_no_open_reload_dom_or_close(self):
        preview, url = await self.registry.preview_existing('one', URL)
        self.assertEqual(url, URL)
        self.assertFalse(self.registry.windows, 'preview is not a connection capability')
        with self.assertRaises(LookupError):
            await self.registry.for_connection('one', URL, preview)
        entry = await self.registry.confirm_existing('one', URL, preview)
        self.assertNotEqual(entry.token, preview)
        self.assertIs(await self.registry.for_connection('one', URL, entry.token), entry)
        await entry.page.context.close()
        self.assertTrue(entry.page.is_closed())
        self.assertFalse(self.runner.closed, 'never physically close an attached existing window')
        with self.assertRaises(LookupError):
            await self.registry.confirm_existing('one', URL, preview)

    async def test_owner_token_target_and_expiry_fail_before_browser_read(self):
        preview, _ = await self.registry.preview_existing('one', URL)
        before = len(self.runner.calls)
        for owner, url, token in [('two', URL, preview), ('one', URL + '2', preview),
                                  ('one', URL, 'forged-token')]:
            with self.assertRaises(LookupError):
                await self.registry.confirm_existing(owner, url, token)
        self.registry.pending[preview].expires_at = 0
        with self.assertRaises(LookupError):
            await self.registry.confirm_existing('one', URL, preview)
        self.assertEqual(len(self.runner.calls), before)

    async def test_changed_url_or_tab_or_closed_window_stops_confirmation(self):
        for kind in ('url', 'tab', 'closed'):
            self.setUp()
            preview, _ = await self.registry.preview_existing('one', URL)
            setattr(self.runner, kind, {'url': URL + '2', 'tab': 2, 'closed': True}[kind])
            with self.assertRaises((ValueError, LookupError)):
                await self.registry.confirm_existing('one', URL, preview)
            self.assertFalse(self.registry.windows)

    async def test_exact_target_and_account_exclusivity(self):
        self.runner.url = 'http://127.0.0.1:5173/'
        with self.assertRaises(ValueError):
            await self.registry.preview_existing('one', URL)
        self.runner.url = URL
        token, _ = await self.registry.preview_existing('one', URL)
        entry = await self.registry.confirm_existing('one', URL, token)
        with self.assertRaises(ValueError):
            await self.registry.preview_existing('two', URL)
        token, _ = await self.registry.preview_existing('one', URL)
        self.assertIs(await self.registry.confirm_existing('one', URL, token), entry)
        self.registry.forget_owner('one')
        self.assertFalse(self.registry.pending)
        self.assertFalse(self.registry.windows)

    async def test_selected_tab_guard_applies_after_connection(self):
        preview, _ = await self.registry.preview_existing('one', URL)
        entry = await self.registry.confirm_existing('one', URL, preview)
        self.runner.tab = 2
        with self.assertRaises(ValueError):
            await entry.page.read_url()


class LocalGestureContract(unittest.TestCase):
    def test_local_origin_guard_and_no_arbitrary_window_id(self):
        with patch('dotenv.load_dotenv'), patch('sqlite3.connect', side_effect=AssertionError('DB forbidden')):
            from app import main
        fixture = FastAPI()
        fixture.add_api_route('/preview', main.preview_existing_safari, methods=['POST'])
        fixture.add_api_route('/confirm', main.confirm_existing_safari, methods=['POST'])
        with TestClient(fixture) as client:
            for path, body in [('/preview', {'url': URL}),
                               ('/confirm', {'url': URL, 'preview_token': 'x' * 32})]:
                self.assertEqual(client.post(path, json=body).status_code, 403)
                self.assertEqual(client.post(path, json=body,
                    headers={'origin': 'https://untrusted.example.test'}).status_code, 403)
                self.assertEqual(client.post(path, json=body,
                    headers={'origin': 'http://127.0.0.1:5173', 'sec-fetch-site': 'cross-site'}).status_code, 403)
                self.assertEqual(client.post(path, json={**body, 'window_id': 271},
                    headers={'origin': 'http://127.0.0.1:5173'}).status_code, 422)
        self.assertNotIn('window_id', ExistingSafariPreview.model_fields)
        self.assertNotIn('window_id', ExistingSafariConfirmation.model_fields)

    def test_success_requires_separate_confirmation_and_fixed_handoff(self):
        with patch('dotenv.load_dotenv'), patch('sqlite3.connect', side_effect=AssertionError('DB forbidden')):
            from app import main
        fixture = FastAPI()
        fixture.add_api_route('/preview', main.preview_existing_safari, methods=['POST'])
        fixture.add_api_route('/confirm', main.confirm_existing_safari, methods=['POST'])
        registry = SimpleNamespace(
            preview_existing=AsyncMock(return_value=('p' * 32, URL)),
            confirm_existing=AsyncMock(return_value=SimpleNamespace(token='c' * 32)))
        sleep = AsyncMock()
        headers = {'origin': 'http://127.0.0.1:5173'}
        with patch.object(main, 'safari_windows', registry), \
                patch.object(main, 'browser_demo', SimpleNamespace(session_id=None, _validate_url=lambda url: url)), \
                patch.object(main, 'configured_browser_engine', return_value='safari'), \
                patch.object(main, 'current_user_id', return_value='fixture-user'), \
                patch.object(main.asyncio, 'sleep', sleep), TestClient(fixture) as client:
            preview = client.post('/preview', json={'url': URL}, headers=headers)
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertNotIn('safari_window_token', preview.json())
            registry.confirm_existing.assert_not_awaited()
            confirmed = client.post('/confirm', headers=headers,
                json={'url': URL, 'preview_token': preview.json()['preview_token']})
            self.assertEqual(confirmed.status_code, 200, confirmed.text)
            self.assertEqual(confirmed.json()['safari_window_token'], 'c' * 32)
            self.assertFalse(confirmed.json()['automation_connected'])
            sleep.assert_awaited_once_with(10)
            registry.preview_existing.assert_awaited_once_with('fixture-user', URL)
            registry.confirm_existing.assert_awaited_once_with('fixture-user', URL, 'p' * 32)


if __name__ == '__main__':
    unittest.main()
