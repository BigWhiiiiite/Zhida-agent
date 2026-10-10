"""Wait for rendered recruiting content, not just a DOMContentLoaded event."""
import asyncio
import time


CONTENT_PROBE = r'''() => {
  const root = document.body;
  if (!root) return false;
  const visible = el => el.getClientRects().length > 0 &&
    getComputedStyle(el).visibility !== 'hidden' && !el.closest('[hidden],[aria-hidden="true"]');
  const text = (root.innerText || '').replace(/\s+/g, '').trim();
  const loadingOnly = /^(?:loading[.。…]*|加载中[.。…]*|正在加载[.。…]*|请稍候[.。…]*)$/i.test(text);
  const controls = [...root.querySelectorAll('input:not([type="hidden"]),select,textarea,button,a[href],[role="button"],[role="combobox"]')].some(visible);
  const images = [...root.querySelectorAll('img')].some(el => visible(el) && el.complete && el.naturalWidth > 40);
  return !loadingOnly && (text.length >= 4 || controls || images);
}'''


async def wait_for_rendered_content(page, timeout_seconds=15):
    deadline = time.monotonic() + timeout_seconds
    consecutive = 0
    while True:
        # Only a boolean leaves the page: no PII, cookies, answers or HTML.
        if await page.evaluate(CONTENT_PROBE):
            consecutive += 1
            if consecutive >= 2:
                return
        else:
            consecutive = 0
        if time.monotonic() >= deadline:
            raise RuntimeError(
                "招聘网页仍未显示内容，已停止分析和填写。请先在浏览器手动确认完整网址能打开，"
                "再重试；这不是主档案或模型识别错误。"
            )
        await asyncio.sleep(0.25)
