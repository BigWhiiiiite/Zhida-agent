"""Autohome upload evidence and the exact, non-submitting parse confirmation."""
from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from playwright.async_api import Page


def _official(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc.casefold() in {
        "talent.autohome.com.cn", "talent.autohome.com.cn:443"
    } and parsed.path == "/recruit-delivery.html"


async def inspect_autohome_attachment(page: Page, previous: dict | None = None) -> dict:
    """Read server-rendered attachment evidence; selected files alone prove nothing.

    The site clears input.files in its upload success callback. A pre-existing
    attachment is reported separately from a newly verified upload. This cannot
    prove parsed fields were applied: the caller must compare field snapshots.
    Signed download URLs are used only to create a fingerprint, never returned.
    """
    unsupported = {"supported": False, "state": "unsupported", "attachment_present": False,
                   "requires_parse_confirmation": False, "upload_verified": False}
    if not _official(page.url):
        return unsupported
    data = await page.evaluate(r"""() => {
      if(location.origin!=='https://talent.autohome.com.cn'||location.pathname!=='/recruit-delivery.html') return null;
      const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
      const visible=el=>{
        if(!el?.getClientRects().length) return false;
        for(let node=el;node;node=node.parentElement){
          const style=getComputedStyle(node);
          if(node.hidden||node.getAttribute('aria-hidden')==='true'||style.display==='none'||
              style.visibility==='hidden'||style.opacity==='0') return false;
        }
        return true;
      };
      const boxes=[...document.querySelectorAll('.delivery_card .jlcard #jlbox')].filter(visible);
      if(boxes.length!==1) return null;
      const box=boxes[0], inputs=box.querySelectorAll('input#uploadFile[type="file"]');
      if(inputs.length!==1) return null;
      const file=inputs[0], link=box.querySelector('a#filename'), icon=box.querySelector('#fileicon');
      const raw=clean(link?.getAttribute('href'));
      let href='';
      try{
        const url=new URL(raw,location.href);
        if(raw&&!raw.startsWith('#')&&['https:','http:'].includes(url.protocol)&&
            url.hostname&&!url.username&&!url.password) href=url.href;
      }catch{}
      const label=clean(link?.innerText);
      const attached=Boolean(box.classList.contains('jlbox')&&visible(link)&&label&&href&&
        visible(icon)&&clean(box.querySelector('#uploadFileLabel')?.innerText)==='更新简历');
      const errorNode=box.querySelector('#file-err-msg');
      const error=visible(errorNode)?clean(errorNode.innerText):'';
      const dialogs=[...document.querySelectorAll('.layui-layer-content')].filter(visible);
      const prompts=dialogs.filter(el=>el.id==='LAY_layuipro2'&&
        clean(el.querySelector(':scope > .alertdiv > .sfbody > .msg')?.innerText)==='是否变更个人信息？');
      let prompt=false;
      if(prompts.length===1&&dialogs.length===1){
        const yes=prompts[0].querySelectorAll('.alertdiv > .sfbody > .option > div.btn2');
        const no=prompts[0].querySelectorAll('.alertdiv > .sfbody > .option > div.cancelbtn');
        prompt=attached&&!error&&yes.length===1&&no.length===1&&visible(yes[0])&&visible(no[0])&&
          clean(yes[0].innerText)==='确定'&&clean(no[0].innerText)==='取消'&&
          /^yesClick\(\);?$/.test(yes[0].getAttribute('onclick')||'')&&
          /^closeClick\(\);?$/.test(no[0].getAttribute('onclick')||'');
      }
      return {supported:true,attachment_present:attached,attachment_label:attached?label:'',
        _href:attached?href:'',error,selected_file_count:file.files?.length||0,
        loading:[...document.querySelectorAll('.layui-layer-loading')].some(visible),
        requires_parse_confirmation:Boolean(prompt),blocking_dialog:dialogs.length>0};
    }""")
    if data is None:
        return unsupported
    href = data.pop("_href")
    data["attachment_fingerprint"] = hashlib.sha256(
        f'{data["attachment_label"]}|{href}'.encode()).hexdigest() if href else ""
    changed = bool(previous and data["attachment_fingerprint"] and
                   data["attachment_fingerprint"] != previous.get("attachment_fingerprint", ""))
    data["upload_verified"] = bool(not data["error"] and not data["loading"] and
                                   (data["requires_parse_confirmation"] or changed))
    data["state"] = ("upload_error" if data["error"] else
                     "parse_confirmation" if data["requires_parse_confirmation"] else
                     "uploading" if data["loading"] and data["selected_file_count"] else
                     "file_selected" if data["selected_file_count"] else
                     "attachment_present" if data["attachment_present"] else "empty")
    return data


async def _respond(page: Page, confirm: bool) -> dict:
    before = await inspect_autohome_attachment(page)
    if not before.get("requires_parse_confirmation"):
        raise ValueError("未识别到唯一的汽车之家简历解析确认框，不会点击通用确定或提交")
    # Bind the click locator to the exact current message as well: this ID is
    # reused by the site's unrelated application and final-confirm dialogs.
    action_class, action_text, handler = ("btn2", "确定", "yesClick") if confirm else ("cancelbtn", "取消", "closeClick")
    selector = ('#LAY_layuipro2:has(> .alertdiv > .sfbody > .msg:text-is("是否变更个人信息？")) '
                f'> .alertdiv > .sfbody > .option > div.{action_class}:text-is("{action_text}")'
                f':is([onclick="{handler}()"],[onclick="{handler}();"])')
    control = page.locator(selector)
    if await control.count() != 1 or not await control.is_visible():
        raise ValueError("简历解析确认控件已变化，请重新核对")
    await control.click(timeout=5000)
    for _ in range(15):
        after = await inspect_autohome_attachment(page, before)
        if after.get("supported") and not after.get("blocking_dialog"):
            if after.get("error") or not after.get("attachment_present"):
                raise ValueError("解析确认后附件证据异常，请核对网页；不会重复点击")
            return {**after, "parse_confirmation_clicked": confirm,
                    "parse_confirmation_cancelled": not confirm,
                    "parse_confirmation_closed": True, "requires_field_verification": confirm}
        await page.wait_for_timeout(100)
    raise ValueError("解析确认框未按预期关闭，已停止且不会重复点击")


async def confirm_autohome_resume_parse(page: Page) -> dict:
    """Accept only '是否变更个人信息？'; caller must recheck every parsed field."""
    return await _respond(page, True)


async def cancel_autohome_resume_parse(page: Page) -> dict:
    """Cancel only the exact resume replacement prompt, retaining the upload."""
    return await _respond(page, False)
