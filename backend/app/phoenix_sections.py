"""Observed Beisen Phoenix repeatable sections, shared across ATS tenants.

Only an own section caption + ux-standard-form records + a scoped Phoenix
text-add control qualify. No company names, array-index binding or submit clicks.
"""
from __future__ import annotations

import hashlib
from urllib.parse import urlparse


CONFIGS = [
    dict(section="教育经历", semantic_section="education", anchor="学校名称"),
    dict(section="实习经历", semantic_section="experience", anchor="单位名称"),
    dict(section="项目经历", semantic_section="project", anchor="项目名称"),
]


def supported(url):
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    return (parsed.scheme == "https" and parsed.port in {None, 443}
            and (host.endswith(".zhiye.com") or host.endswith(".italent.cn"))
            and parsed.path.rstrip("/") == "/form")


async def inspect_phoenix_sections(page):
    if not supported(page.url):
        return []
    rows = await page.evaluate(r"""configs => {
      const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
      const visible=el=>{
        if(!el?.getClientRects().length)return false;
        for(let n=el;n;n=n.parentElement){const s=getComputedStyle(n);
          if(n.hidden||n.getAttribute('aria-hidden')==='true'||s.display==='none'||
             s.visibility==='hidden'||s.opacity==='0')return false;
        }return true;
      };
      const controls='input,select,textarea,.phoenix-select,.phoenix-radio';
      const ownCaption=el=>{
        const copy=el.cloneNode(true);copy.querySelectorAll('svg,script,style,[hidden],[aria-hidden="true"]').forEach(n=>n.remove());
        return clean(copy.textContent);
      };
      const sections=new Map();
      for(const record of document.querySelectorAll('.ux-standard-form')){
        if(!visible(record))continue;
        for(let n=record,d=0;n&&d<8&&n!==document.body;n=n.parentElement,d++){
          const owner=n.parentElement;if(!owner)break;
          const captions=[...owner.children].filter(c=>c!==n&&visible(c)&&
            !c.querySelector(controls)&&!c.querySelector('.ux-standard-form')).map(c=>clean(c.innerText));
          const configsHere=configs.filter(config=>captions.includes(config.section));
          if(configsHere.length>1)break;
          if(configsHere.length===1){sections.set(owner,configsHere[0]);break;}
        }
      }
      const rows=[];
      for(const [owner,config] of sections){
        const records=[...owner.querySelectorAll('.ux-standard-form')].filter(visible);
        if(!records.length||records.length>30)continue;
        let valid=true;
        for(const record of records){
          const anchors=[...record.querySelectorAll('.form-item.form-item--phoenix')].filter(q=>
            q.closest('.ux-standard-form')===record&&visible(q)&&
            clean(q.querySelector(':scope > .form-item__title')?.innerText)===config.anchor);
          const inputs=anchors.length===1?[...anchors[0].querySelectorAll('input,textarea')]
            .filter(el=>visible(el)&&!el.disabled&&!el.readOnly&&el.type!=='hidden'):[];
          if(inputs.length!==1)valid=false;
        }
        if(!valid)continue;
        if(!owner.dataset.zhidaSection)owner.dataset.zhidaSection='phoenix-section-'+crypto.randomUUID();
        for(const record of records)if(!record.dataset.zhidaRecord)
          record.dataset.zhidaRecord='phoenix-record-'+crypto.randomUUID();
        const expected='添加'+config.section;
        const adds=[...owner.querySelectorAll('.phoenix-text-button')].filter(el=>
          visible(el)&&!el.closest('.ux-standard-form,header,nav,[role="dialog"]')&&
          !el.matches('[disabled],[aria-disabled="true"],.phoenix-text-button--disabled')&&
          !el.closest('[hidden],[aria-hidden="true"],[inert],[aria-disabled="true"]')&&
          !el.querySelector('input,select,textarea,a')&&!el.hasAttribute('href')&&
          !el.hasAttribute('onclick')&&clean(el.innerText)===expected&&
          clean(el.querySelector(':scope > .phoenix-text-button__textWraper')?.innerText)===expected&&
          el.querySelector(':scope > .phoenix-text-button__preIcon'));
        // The live Phoenix portal renders additions as a separate plain
        // record-list sibling, not phoenix-text-button (that class is used
        // for attachment uploads). Match the observed ownership relationship,
        // not generated styled-component classes or global text.
        if(!adds.length){
          const fallback=new Set();
          for(const record of records){
            for(let n=record,d=0;n&&n!==owner&&d<8;n=n.parentElement,d++){
              const parent=n.parentElement;if(!parent||!owner.contains(parent))break;
              for(const sibling of parent.children){
                if(sibling===n||!visible(sibling)||sibling.querySelector(controls)||
                   sibling.querySelector('.ux-standard-form,.phoenix-text-button')||
                   sibling.matches('.phoenix-text-button')||ownCaption(sibling)!==expected)continue;
                const leaves=[sibling,...sibling.querySelectorAll('div,span')].filter(el=>
                  visible(el)&&ownCaption(el)===expected&&
                  ![...el.children].some(c=>visible(c)&&ownCaption(c)===expected));
                if(leaves.length!==1)continue;
                const leaf=leaves[0];
                if(!leaf.matches('div,span')||leaf.closest('.ux-standard-form,header,nav,[role="dialog"]')||
                   sibling.querySelector('input,select,textarea,a,button,[role="button"],form')||
                   sibling.matches('a,button,[disabled],[aria-disabled="true"]')||
                   sibling.hasAttribute('href')||sibling.hasAttribute('onclick')||
                   leaf.hasAttribute('href')||leaf.hasAttribute('onclick')||
                   leaf.closest('[hidden],[aria-hidden="true"],[inert],[aria-disabled="true"]'))continue;
                fallback.add(leaf);
              }
            }
          }
          adds.push(...fallback);
        }
        const add=adds.length===1?adds[0]:null;
        if(add&&!add.dataset.zhidaAdd)add.dataset.zhidaAdd='phoenix-add-'+crypto.randomUUID();
        rows.push({...config,label:expected,container_key:owner.dataset.zhidaSection,
          selector:add?'[data-zhida-add="'+add.dataset.zhidaAdd+'"]':'',
          record_count:records.length,record_keys:records.map(r=>r.dataset.zhidaRecord)});
      }
      // Two equally titled sections are ambiguous, even with different owners.
      return rows.filter(row=>rows.filter(r=>r.semantic_section===row.semantic_section).length===1);
    }""", CONFIGS)
    for row in rows:
        row["id"] = hashlib.sha256(
            f'{page.url}|{row["container_key"]}|{row["record_keys"]}|{row["selector"]}'.encode()
        ).hexdigest()[:24]
        row["field_type"] = "section-button"
        row["section_path"] = [row["section"]]
    return rows


async def discover_phoenix_sections(page):
    return [row for row in await inspect_phoenix_sections(page)
            if row["selector"] and row["record_count"] < 30]


async def expand_phoenix_section(page, candidate_id):
    before = await inspect_phoenix_sections(page)
    chosen = next((r for r in before if r["id"] == candidate_id and r["selector"]
                   and r["record_count"] < 30), None)
    if not chosen:
        raise ValueError("当前没有可核验的新增经历入口，请重新识别")
    url = page.url
    control = page.locator(chosen["selector"])
    if await control.count() != 1 or not await control.is_visible():
        raise ValueError("新增经历控件已变化，未点击")
    await control.click(timeout=5000)  # exactly once, including delayed/no-op sites
    previous = {r["container_key"]: set(r["record_keys"]) for r in before}
    for _ in range(15):
        if page.url != url:
            break
        after = await inspect_phoenix_sections(page)
        current = {r["container_key"]: set(r["record_keys"]) for r in after}
        if set(current) == set(previous):
            key = chosen["container_key"]
            other_unchanged = all(current[k] == previous[k] for k in previous if k != key)
            if other_unchanged and previous[key] < current[key] and len(current[key]-previous[key]) == 1:
                row = next(r for r in after if r["container_key"] == key)
                return {**row, "previous_count":chosen["record_count"], "added_count":1}
            if any(not previous[k].issubset(current[k]) or len(current[k]-previous[k]) > (k == key)
                   for k in previous):
                break
        await page.wait_for_timeout(100)
    raise ValueError("新增后没有验证到仅目标栏目增加一条，已停止，不会重复点击")
