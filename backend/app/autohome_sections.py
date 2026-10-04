"""Observe and expand one verified Autohome experience record, never submit."""
from __future__ import annotations

import hashlib
from urllib.parse import urlparse

from playwright.async_api import Page


_SECTIONS = [
    {"kind": "education", "title": "教育经历", "template": "eduTemplate", "handler": "addEdu", "anchor": "School"},
    {"kind": "experience", "title": "实习/工作经历", "template": "workTemplate", "handler": "addWork", "anchor": "Company"},
    {"kind": "project", "title": "项目经历", "template": "projectTemplate", "handler": "addProject", "anchor": "ProjectName"},
]


def _official(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc.casefold() in {
        "talent.autohome.com.cn", "talent.autohome.com.cn:443"
    } and parsed.path == "/recruit-delivery.html"


async def _observe(page: Page) -> list[dict]:
    if not _official(page.url):
        return []
    rows = await page.evaluate(r"""configs=>{
      if(location.origin!=='https://talent.autohome.com.cn'||location.pathname!=='/recruit-delivery.html') return [];
      const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
      const visible=el=>{
        if(!el?.getClientRects().length) return false;
        for(let n=el;n;n=n.parentElement){
          const s=getComputedStyle(n);
          if(n.hidden||n.getAttribute('aria-hidden')==='true'||s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
        }
        return true;
      };
      const path=el=>{
        const parts=[];
        for(let n=el;n&&n.tagName!=='HTML';n=n.parentElement)
          parts.unshift(n.tagName.toLowerCase()+':nth-child('+([...n.parentElement.children].indexOf(n)+1)+')');
        return 'html > '+parts.join(' > ');
      };
      const cards=[...document.querySelectorAll('form.validform .icard')];
      const allCards=[...document.querySelectorAll('.icard')];
      const rows=[];
      for(const config of configs){
        const matches=cards.filter(card=>visible(card)&&clean(card.querySelector(':scope > .title')?.innerText)===config.title);
        if(matches.length!==1) continue;
        const card=matches[0];
        const roots=[...card.querySelectorAll('div[data-bind]')].filter(el=>
          new RegExp("name\\s*:\\s*['\"]"+config.template+"['\"]").test(el.getAttribute('data-bind')||''));
        if(roots.length!==1) continue;
        const root=roots[0], children=[...root.children];
        const ends=children.filter(n=>n.matches('.option'));
        const anchors=[...root.querySelectorAll('input[data-bind]')].filter(el=>
          new RegExp('(?:^|,)\\s*value\\s*:\\s*'+config.anchor+'\\s*(?:,|$)').test(el.getAttribute('data-bind')||''));
        if(anchors.length!==ends.length||anchors.length>30) continue;
        // Every record ends at its own .option. Do not count captions, dates,
        // hidden template scripts or another record's repeated add control.
        let start=0, valid=true;
        for(const end of ends){
          const stop=children.indexOf(end), segment=children.slice(start,stop);
          if(anchors.filter(el=>segment.some(node=>node.contains(el))).length!==1) valid=false;
          start=stop+1;
        }
        if(!valid) continue;
        const source=anchors.length?root:card.querySelector(':scope > .content > .contentFirstRow');
        const selector=anchors.length?':scope > .option > div.add':':scope > div.addbtn';
        const adds=[...(source?.querySelectorAll(selector)||[])].filter(el=>visible(el)&&
          clean(el.innerText)==='增加'+config.title&&!el.hasAttribute('onclick')&&
          !el.matches('.del,.save,.cancel')&&!el.querySelector('a,button,input')&&
          new RegExp('(?:^|,)\\s*click\\s*:\\s*(?:\\$parent\\.)?'+config.handler+'\\s*(?:,|$)').test(el.getAttribute('data-bind')||''));
        const add=adds.length?adds[adds.length-1]:null;
        const index=allCards.indexOf(card), key=`autohome:${config.kind}:${index}`;
        rows.push({section:config.title,semantic_section:config.kind,label:'增加'+config.title,
          selector:add?path(add):'',root_selector:path(root),record_count:anchors.length,
          container_key:key,record_keys:anchors.map((_,i)=>key+':'+(i+1))});
      }
      return rows;
    }""", _SECTIONS)
    for row in rows:
        row["id"] = hashlib.sha256(
            f'{page.url}|{row["container_key"]}|{row["record_count"]}|{row["selector"]}'.encode()
        ).hexdigest()[:24]
        row["field_type"] = "section-button"
        row["section_path"] = [row["section"]]
    return rows


async def discover_autohome_sections(page: Page) -> list[dict]:
    """Return current, observed add candidates without changing the DOM."""
    return [row for row in await _observe(page) if row["selector"] and row["record_count"] < 30]


async def expand_autohome_section(page: Page, candidate_id: str) -> dict:
    """Click exactly once; require one new record and unchanged other counts.

    Caller must request another fresh candidate to add another record. A failed
    verification raises without retrying, deleting or attempting a rollback.
    """
    before = await _observe(page)
    chosen = next((row for row in before if row["id"] == candidate_id and row["selector"]
                   and row["record_count"] < 30), None)
    if not chosen:
        raise ValueError("没有当前可核验的汽车之家新增经历入口，请重新识别")
    control = page.locator(chosen["selector"])
    if await control.count() != 1 or not await control.is_visible():
        raise ValueError("新增经历控件已变化，请重新识别")
    await control.click(timeout=5000)
    expected = {row["container_key"]: row["record_count"] for row in before}
    expected[chosen["container_key"]] += 1
    for _ in range(15):
        after = await _observe(page)
        counts = {row["container_key"]: row["record_count"] for row in after}
        if counts == expected:
            result = next(row for row in after if row["container_key"] == chosen["container_key"])
            return {**result, "previous_count": chosen["record_count"], "added_count": 1}
        if any(counts.get(key, count) > count for key, count in expected.items()):
            break
        await page.wait_for_timeout(100)
    raise ValueError("新增经历后未观察到仅目标分组增加一条，已停止且不会重复点击")
