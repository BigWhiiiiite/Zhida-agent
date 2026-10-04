"""Metadata for the observed Beisen Phoenix template, shared across tenants.

No company/job specific answers, no DOM-order-to-resume binding. A complete
ux-standard-form owns a record, including description blocks split by the ATS.
"""
from __future__ import annotations

from .field_semantics import education_level_hint


CHOICE_STATE_JS = r"""el => {
  if ('checked' in el) return String(Boolean(el.checked));
  const aria=el.getAttribute('aria-checked');
  if(aria==='true'||aria==='false')return aria;
  if(!el.matches('.phoenix-radio'))return '';
  const dot=el.querySelector('.phoenix-radio__dot');
  if(!dot)return '';
  const style=getComputedStyle(dot);
  if(style.display==='none'||style.visibility==='hidden'||Number(style.opacity)===0)return 'false';
  const matrix=/^matrix\(([^)]+)\)$/.exec(style.transform);
  if(matrix){const n=matrix[1].split(',').map(Number);if(n.length===6&&n[0]===0&&n[3]===0)return 'false';}
  // Only the actual visible dot proves selection; option text never does.
  return dot.getClientRects().length ? 'true' : 'false';
}"""


PHOENIX_SELECT_VALUE_JS = r"""el => {
  const content=el.querySelector('.phoenix-select__content');
  return content ? String(content.innerText||'').replace(/\s+/g,' ').trim() : '';
}"""


async def refine_phoenix_fields(page, data: list[dict]) -> list[dict]:
    patches = await page.evaluate(r"""selectors => {
      const clean=v=>String(v||'').replace(/\s+/g,' ').trim();
      const visible=n=>n&&n.getClientRects().length&&!n.closest('[hidden],[aria-hidden="true"]')&&
        getComputedStyle(n).display!=='none'&&getComputedStyle(n).visibility!=='hidden';
      const controls='input,select,textarea,.phoenix-radio,.phoenix-select';
      const names=new Set(['个人信息','教育经历','实习经历','项目经历','语言能力','获奖情况','简历附件','附件']);
      const sectionFor=record=>{
        for(let n=record,d=0;n&&d<8&&n!==document.body;n=n.parentElement,d++){
          const siblings=[...(n.parentElement?.children||[])].filter(c=>c!==n&&!c.querySelector(controls)&&visible(c));
          const titles=[...new Set(siblings.map(c=>clean(c.innerText)).filter(t=>names.has(t)))];
          if(titles.length===1)return titles[0];
          if(titles.length>1)return '';
        }return '';
      };
      const kindFor={'教育经历':'education','实习经历':'experience','项目经历':'project','语言能力':'language'};
      const map={
        education:{'开始时间':'start_date','结束时间':'end_date','学校名称':'school','专业名称':'major','学历':'degree'},
        experience:{'开始时间':'start_date','结束时间':'end_date','至今':'current','单位名称':'organization','职位名称':'role','实习地点':'location','实习内容':'description'},
        project:{'开始时间':'start_date','结束时间':'end_date','至今':'current','项目名称':'name','职务':'role','项目描述':'description','项目中职责':'responsibilities'},
        language:{'语言类型':'name','掌握程度':'proficiency','听说':'speaking','读写':'writing'}
      };
      return selectors.map(selector=>{
        const matches=document.querySelectorAll(selector);if(matches.length!==1)return null;
        const el=matches[0],record=el.closest('.ux-standard-form');
        let q=el.closest('.form-item.form-item--phoenix');
        // The observed Phoenix template renders 至今 BESIDE the end-date
        // question, not inside it. Only its smallest single-question owner
        // within this record can prove that relationship; never borrow a
        // neighboring record's date/title or a page-wide checkbox caption.
        const checkbox=el.matches('input[type="checkbox"].phoenix-checkbox__input');
        const ownCheckbox=checkbox?clean(el.closest('.phoenix-checkbox')?.querySelector('.phoenix-checkbox__text')?.innerText):'';
        if(!q&&record&&ownCheckbox==='至今'){
          for(let n=el.parentElement,d=0;n&&n!==record&&d<5;n=n.parentElement,d++){
            const questions=[...n.querySelectorAll('.form-item.form-item--phoenix')].filter(visible);
            const checks=[...n.querySelectorAll('input[type="checkbox"]')].filter(visible);
            if(questions.length===1&&checks.length===1&&checks[0]===el&&
               clean(questions[0].querySelector(':scope > .form-item__title')?.innerText)==='结束时间'){
              q=questions[0];break;
            }
          }
        }
        if(!q||!record||!visible(q)||!record.contains(q))return null;
        const title=q.querySelector(':scope > .form-item__title');if(!title)return null;
        let label=clean(title.innerText);if(!label||label.length>220)return null;
        // The end-date question also owns an independent "至今" checkbox.
        // It is not a boolean version of the end-date input.
        if(el.matches('input[type="checkbox"]')){
          const own=clean(el.closest('.phoenix-checkbox')?.querySelector('.phoenix-checkbox__text')?.innerText);
          if(!own)return null;
          label=own;
        }
        const section=sectionFor(record),kind=kindFor[section]||'';
        if(!record.dataset.zhidaRecord)record.dataset.zhidaRecord='phoenix-record-'+crypto.randomUUID();
        const key=record.dataset.zhidaRecord;
        const required=Boolean(el.required||el.getAttribute('aria-required')==='true'||
          (!checkbox&&([...title.querySelectorAll('.form-item__required')].some(visible)||
          [...title.querySelectorAll('.form-item__text')].some(n=>/[*＊]/.test(getComputedStyle(n,'::before').content)))));
        const patch={label,question_text:label,label_source:'container-owned',recognition_confidence:.99,
          required,section,section_path:section?[section]:[],context:[section,label].filter(Boolean).join(' / '),
          nearby_labels:[label],group_label:label,container_key:key};
        if(kind&&map[kind][label]){patch.semantic_key=kind+'.'+map[kind][label];patch.entity_scope=kind+':unspecified';}
        if(kind==='education'){
          const degrees=[...record.querySelectorAll('.form-item.form-item--phoenix')].filter(n=>
            clean(n.querySelector(':scope > .form-item__title')?.innerText)==='学历');
          if(degrees.length===1){patch._degree=clean(degrees[0].querySelector('.phoenix-select__content')?.innerText);}
        }
        if(kind==='language'&&map.language[label]){
          const types=[...record.querySelectorAll('.form-item.form-item--phoenix')].filter(n=>visible(n)&&
            clean(n.querySelector(':scope > .form-item__title')?.innerText)==='语言类型');
          if(types.length===1){
            const selected=clean(types[0].querySelector('.phoenix-select__content')?.innerText);
            // Only this record's committed type owns its self-assessments.
            // A sibling language, a search string or IELTS score cannot do so.
            const known=new Set(['中文','英语','日语','韩语','法语','德语','西班牙语','意大利语','阿拉伯语','俄语']);
            if(known.has(selected))patch.entity_scope='language:'+selected;
          }
        }
        // Search text is not a committed selection. Popup inputs are excluded.
        if(el.matches('.phoenix-select'))patch.current_value=clean(el.querySelector('.phoenix-select__content')?.innerText);
        if(el.matches('.phoenix-radio'))patch.option_label=clean(el.querySelector('.phoenix-radio__radio-text')?.innerText);
        return patch;
      });
    }""", [item['selector'] for item in data])
    result = []
    for item, patch in zip(data, patches):
        if not patch:
            result.append(item)
            continue
        degree = patch.pop('_degree', '')
        if degree and patch.get('entity_scope') == 'education:unspecified':
            level = education_level_hint(degree)
            if level:
                patch['entity_scope'] = 'education:' + level
        if item.get('field_type') == 'radio':
            control = page.locator(item['selector'])
            if await control.evaluate("el=>el.matches('.phoenix-radio')"):
                patch['current_value'] = await control.evaluate(CHOICE_STATE_JS)
        result.append({**item, **patch})
    return result
