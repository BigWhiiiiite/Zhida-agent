"""Bounded, read-only observations. Images never enter storage or logs.

Use Playwright's existing ARIA/screenshot APIs or a scoped macOS bridge, not
the Codex desktop tool. A failed image/AX capability is reported explicitly.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import re
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw
from pydantic import BaseModel, Field


class PageObservationRequest(BaseModel):
    model_config = {"extra": "forbid"}
    selector: str = Field(min_length=1, max_length=1000)
    include_image: bool = False
    context_token: str = Field(min_length=64, max_length=64)


class ObservationConsentRequest(BaseModel):
    model_config = {"extra": "forbid"}
    enabled: bool
    context_token: str = Field(min_length=64, max_length=64)


class PageRegionObservation(BaseModel):
    selector: str
    scope: str = "current_question_visible_region"
    accessibility_source: str = "dom_aria"
    accessibility: str = ""
    context: dict = Field(default_factory=dict)
    image_data_url: str = ""
    limitations: list[str] = Field(default_factory=list)
    read_only: bool = True
    # No assertion that all personal data has been removed from an image.
    privacy_note: str = "填写控件与照片区域会遮挡；题干和其他网页文字仍可能包含个人资料。"


# Constant code only, with the exact scanner-issued selector as data. No scroll,
# click, menu opening, value writes, storage access, or arbitrary model scripts.
REGION_SCRIPT = r"""selector => {
  const matches = document.querySelectorAll(selector);
  if (matches.length !== 1) throw new Error('ZHIDA_OBSERVATION_TARGET_CHANGED');
  const el = matches[0];
  const rect = e => { const r=e.getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}; };
  const visible = e => {const r=rect(e),s=getComputedStyle(e);return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
  const editable = 'input,textarea,select,[role="textbox"],[role="combobox"],[contenteditable="true"]';
  let root = el;
  // Keep a small question owner, never promote to a page/form/record container.
  for(let p=el.parentElement,n=0;p&&n<5;p=p.parentElement,n++) {
    if(['BODY','HTML','FORM'].includes(p.tagName)) break;
    const r=rect(p), controls=Array.from(p.querySelectorAll(editable));
    const choiceGroup=controls.length>1&&controls.every(c=>['radio','checkbox'].includes(c.type))
      &&el.name&&controls.every(c=>c.name===el.name&&c.type===el.type)
      &&p.matches('fieldset,[role="group"],[role="radiogroup"],.ant-form-item,.el-form-item,.form-group');
    if(r.width>1100||r.height>450||(controls.length>1&&!choiceGroup)) break;
    root=p;
    if(p.matches('fieldset,[role="group"],[role="radiogroup"],.ant-form-item,.el-form-item,.form-group')) break;
  }
  const r=rect(root),vw=innerWidth,vh=innerHeight;
  const x=Math.max(0,r.x-8),y=Math.max(0,r.y-8);
  const crop={x,y,width:Math.min(vw,r.x+r.width+8)-x,height:Math.min(vh,r.y+r.height+8)-y};
  if(!visible(el)||crop.width<10||crop.height<10) return {unavailable:'题目不在当前可见区域；请在招聘页滚动到这道题后重试。本工具不会自动滚动。'};
  const values=Array.from(document.querySelectorAll(editable)).flatMap(e=>{
    // Passwords and OTPs are never read even for redaction.
    if(e.type==='password'||/password|passcode|captcha|verification|one-time-code|one[_ ]time[_ ]code|验证码|密码|\botp\b/i.test([e.name,e.id,e.autocomplete].join(' '))) return [];
    if(e.tagName==='SELECT') return Array.from(e.selectedOptions).map(o=>o.textContent||'');
    const v=e.value;
    return typeof v==='string'&&v.trim()?[v]:[];
  }).filter(v=>v.length>1).slice(0,400);
  const scrub = text => {
    let s=String(text||'');
    for(const v of values.sort((a,b)=>b.length-a.length)) s=s.split(v).join('[已遮挡]');
    return s.replace(/[\w.+-]+@[\w.-]+\.[a-z]{2,}/gi,'[邮箱已遮挡]').replace(/\b1[3-9]\d{9}\b/g,'[手机号已遮挡]').slice(0,1600);
  };
  const masks=Array.from(document.querySelectorAll(editable+',img,canvas,iframe,video,[role="checkbox"],[role="radio"]')).filter(visible).map(rect);
  const tree=Array.from(new Set([root,...root.querySelectorAll('label,legend,[role],input,textarea,select,button')])).filter(visible).slice(0,80).map(e=>({
    role:e.getAttribute('role')||e.tagName.toLowerCase(),
    name:scrub(e.getAttribute('aria-label')||e.getAttribute('placeholder')||(e.matches('label,legend,button')?e.textContent:'')),
    input_type:e.matches('input')?e.type:'',
    required:e.required===true||e.getAttribute('aria-required')==='true',
    expanded:e.getAttribute('aria-expanded'),disabled:e.disabled===true,
    // Deliberately omit value, checked, selected, href and arbitrary attributes.
  }));
  const labels=Array.from(root.querySelectorAll('label,legend')).map(e=>scrub(e.textContent)).filter(Boolean);
  return {crop,viewport:{width:vw,height:vh},masks,redactions:values,
    context:{labels,accessible_nodes:tree,context_only:true},
    document_stamp:[location.href,performance.timeOrigin,scrollX,scrollY,vw,vh],
    target_stamp:[el.tagName,el.getAttribute('name'),el.getAttribute('type'),el.getAttribute('role'),
      rect(el),el.outerHTML.slice(0,6000)]};
}"""


def redact_text(text: str, values: list[str]) -> str:
    text = re.sub(r"(?m)^\s*-?\s*/url:.*$", "", text)
    for value in sorted(set(values), key=len, reverse=True):
        if len(value) > 1:
            text = text.replace(value, "[已遮挡]")
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[a-z]{2,}", "[邮箱已遮挡]", text, flags=re.I)
    return re.sub(r"\b1[3-9]\d{9}\b", "[手机号已遮挡]", text)[:9000]


def masked_image(raw: bytes, region: dict) -> str:
    if len(raw) > 8_000_000:
        raise ValueError("截图超出只读观察大小限制")
    with Image.open(io.BytesIO(raw)) as source:
        if source.width * source.height > 8_000_000:
            raise ValueError("截图超出只读观察像素限制")
        pic = source.convert("RGB")
    crop = region["crop"]
    sx, sy = pic.width / crop["width"], pic.height / crop["height"]
    draw = ImageDraw.Draw(pic)
    for box in region["masks"]:
        # Cover the entire value-bearing control, including selected/custom text.
        left, top = (box["x"] - crop["x"]) * sx, (box["y"] - crop["y"]) * sy
        right, bottom = left + box["width"] * sx, top + box["height"] * sy
        if right >= 0 and bottom >= 0 and left < pic.width and top < pic.height:
            draw.rectangle((max(0, left - 3), max(0, top - 3),
                            min(pic.width, right + 3), min(pic.height, bottom + 3)), fill="#64746c")
    pic.thumbnail((1200, 600))
    out = io.BytesIO()
    pic.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode("ascii")


async def _process(argv: list[str], *, payload: bytes = b"", timeout: int = 20) -> bytes:
    proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(payload), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    if proc.returncode:
        # Neither native errors, webpage text nor compiler stderr enter logs/API.
        raise RuntimeError("macOS 只读观察接口暂不可用；保留 DOM 观察结果")
    return out


_native_build_lock = None  # Python 3.9: initialize inside the running loop.
_native_directory = None
_native_binary = None


async def native_binary() -> str:
    global _native_directory, _native_binary, _native_build_lock
    if _native_build_lock is None:
        _native_build_lock = asyncio.Lock()
    async with _native_build_lock:
        if _native_binary:
            return _native_binary
        helper = Path(__file__).resolve().parent / "native" / "page_observer.swift"
        folder = tempfile.TemporaryDirectory(prefix="zhida-observer-")
        binary = str(Path(folder.name) / "observer")
        try:
            await _process(["/usr/bin/swiftc", "-parse-as-library", "-module-cache-path",
                            str(Path(folder.name) / "modules"), str(helper), "-o", binary], timeout=45)
        except BaseException:
            folder.cleanup()
            raise
        _native_directory, _native_binary = folder, binary
        return binary


async def _safari_binding(page) -> str:
    return await page._command('''if (index of current tab of window id windowId) is not tabIndex then error "ZHIDA_TAB_CHANGED"
      set b to bounds of window id windowId
      return (item 1 of b as text) & "|" & (item 2 of b as text) & "|" & (item 3 of b as text) & "|" & (item 4 of b as text)''')


async def safari_observation(page, region: dict, include_image: bool) -> dict:
    if sys.platform != "darwin":
        return {"limitation": "Safari 原生可访问树仅支持本机 macOS"}
    # Read only the recorded window; confirm the bound tab is selected. Never
    # capture whatever happens to be the user's frontmost window.
    binding = await _safari_binding(page)
    bounds = [int(part) for part in binding.split("|")]
    if len(bounds) != 4:
        raise ValueError("招聘窗口归属不明确，未读取画面")
    # One lazy build in a private temporary folder; no dependency download, OS
    # preference writes, unmasked screenshot file, or persistent image cache.
    binary = await native_binary()
    payload = {"bounds": bounds, "url": page.url, "viewport": region["viewport"],
               "crop": region["crop"], "include_image": include_image}
    output = await _process([binary], payload=json.dumps(payload).encode(), timeout=15)
    if binding != await _safari_binding(page):
        raise ValueError("观察期间招聘标签页或窗口位置变化，已丢弃画面")
    return json.loads(output)


async def observe_region(page, selector: str, *, include_image: bool = False) -> PageRegionObservation:
    before = await page.evaluate(REGION_SCRIPT, selector)
    if before.get('unavailable'):
        return PageRegionObservation(selector=selector, limitations=[before['unavailable']])
    stamp = json.dumps([before[k] for k in ('document_stamp','target_stamp','crop','masks','redactions','context')], sort_keys=True)
    result = PageRegionObservation(selector=selector, context=before["context"],
                                   accessibility=json.dumps(before["context"]["accessible_nodes"], ensure_ascii=False))
    raw = None
    if hasattr(page, "window_id"):
        try:
            native = await safari_observation(page, before, include_image)
            if native.get("accessibility"):
                result.accessibility_source = "macos_ax"
                result.accessibility = redact_text(json.dumps(native["accessibility"], ensure_ascii=False), before["redactions"])
            if native.get("image_base64"):
                raw = base64.b64decode(native["image_base64"], validate=True)
            if native.get("limitation"):
                result.limitations.append(native["limitation"])
        except (RuntimeError, asyncio.TimeoutError):
            result.limitations.append("Safari 原生观察不可用；未请求系统授权，保留 DOM/ARIA 证据。")
    else:
        locator = page.locator(selector)
        try:
            result.accessibility = redact_text(await locator.aria_snapshot(timeout=3000), before["redactions"])
            result.accessibility_source = "playwright_aria"
        except Exception:
            result.limitations.append("Playwright 可访问快照未读取成功，保留 DOM/ARIA 证据。")
        if include_image:
            try:
                raw = await page.screenshot(type="png", clip=before["crop"], scale="css", timeout=5000,
                    mask=[page.locator('input,textarea,select,[role="textbox"],[role="combobox"],[contenteditable="true"],img,canvas,iframe,video,[role="checkbox"],[role="radio"]')],
                    mask_color="#64746c")
            except Exception:
                result.limitations.append("当前题目截图不可用，未退回整个桌面或整页截图。")
    after = await page.evaluate(REGION_SCRIPT, selector)
    fresh_stamp = json.dumps([after[k] for k in ('document_stamp','target_stamp','crop','masks','redactions','context')], sort_keys=True)
    if hashlib.sha256(stamp.encode()).digest() != hashlib.sha256(fresh_stamp.encode()).digest():
        raise ValueError("观察期间网页、题目或可见区域变化，已丢弃观察结果，请重新同步")
    if raw:
        result.image_data_url = masked_image(raw, before)
    elif include_image:
        result.limitations.append("没有取得可验证的题目画面；不据此宣称视觉识别成功。")
    result.limitations.append("只观察当前可见题目；不证明 iframe、折叠项或后续页面已完整读取。")
    return result
