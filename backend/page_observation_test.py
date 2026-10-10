"""Offline anonymous fixtures. No real browser, OS permission or model calls."""
import copy
import io
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from app.page_observation import masked_image, observe_region, redact_text


def region():
    return {'crop': {'x': 0, 'y': 0, 'width': 120, 'height': 80},
            'viewport': {'width': 1000, 'height': 800},
            'masks': [{'x': 10, 'y': 20, 'width': 40, 'height': 20}],
            'redactions': ['fixture candidate', 'fixture@example.com'],
            'context': {'labels': ['专业'], 'accessible_nodes': [{'role': 'textbox', 'name': '专业'}],
                        'context_only': True},
            'document_stamp': ['https://jobs.example.com/form', 1, 0, 0, 1000, 800],
            'target_stamp': ['INPUT', 'major', 'text', None, {}, '<input id="major">']}


class FakePage:
    def __init__(self):
        self.values = [region(), region()]
        self.screenshot = AsyncMock(return_value=png())
        self.aria_snapshot = AsyncMock(return_value='- textbox "专业": fixture candidate')

    async def evaluate(self, expression, selector):
        return copy.deepcopy(self.values.pop(0))

    def locator(self, selector):
        return self


def png():
    output = io.BytesIO()
    Image.new('RGB', (120, 80), 'white').save(output, format='PNG')
    return output.getvalue()


class ObservationSafety(unittest.IsolatedAsyncioTestCase):
    async def test_default_does_not_capture_image(self):
        page = FakePage()
        result = await observe_region(page, '#major')
        page.screenshot.assert_not_awaited()
        self.assertFalse(result.image_data_url)
        self.assertEqual(result.accessibility_source, 'playwright_aria')
        self.assertNotIn('fixture candidate', result.accessibility)

    async def test_image_request_masks_controls_and_returns_only_crop(self):
        page = FakePage()
        result = await observe_region(page, '#major', include_image=True)
        self.assertTrue(result.image_data_url.startswith('data:image/png;base64,'))
        self.assertEqual(page.screenshot.await_args.kwargs['clip'], region()['crop'])
        self.assertIn('mask', page.screenshot.await_args.kwargs)

    async def test_changed_document_discards_result(self):
        page = FakePage()
        page.values[1]['document_stamp'][1] = 2
        with self.assertRaisesRegex(ValueError, '已丢弃'):
            await observe_region(page, '#major', include_image=True)

    async def test_changed_neighbor_value_discards_result(self):
        page = FakePage()
        page.values[1]['redactions'].append('new value')
        with self.assertRaises(ValueError):
            await observe_region(page, '#major')

    async def test_offscreen_does_not_capture_or_move(self):
        page = FakePage()
        page.values = [{'unavailable': '题目不在当前可见区域'}]
        result = await observe_region(page, '#major', include_image=True)
        self.assertFalse(result.image_data_url)
        page.screenshot.assert_not_awaited()
        page.aria_snapshot.assert_not_awaited()

    async def test_native_permission_failure_does_not_capture_desktop(self):
        page = FakePage()
        page.window_id = 123
        with patch('app.page_observation.safari_observation', new=AsyncMock(
                return_value={'limitation': '需要辅助功能授权'})):
            result = await observe_region(page, '#major', include_image=True)
        self.assertEqual(result.accessibility_source, 'dom_aria')
        self.assertFalse(result.image_data_url)
        page.screenshot.assert_not_awaited()

    def test_redact_values_and_urls(self):
        text = '- textbox "专业": fixture candidate\n- /url: https://example.com/?token=secret\nfixture@example.com 13800000000'
        safe = redact_text(text, ['fixture candidate'])
        for value in ('fixture candidate', 'fixture@example.com', '13800000000', 'token=secret'):
            self.assertNotIn(value, safe)

    def test_mask_geometry(self):
        import base64
        image = masked_image(png(), region()).split(',', 1)[1]
        with Image.open(io.BytesIO(base64.b64decode(image))) as picture:
            self.assertEqual(picture.getpixel((20, 30)), (100, 116, 108))
            self.assertEqual(picture.getpixel((100, 5)), (255, 255, 255))


if __name__ == '__main__':
    unittest.main()
